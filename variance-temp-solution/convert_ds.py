import csv
import json
import pathlib
from decimal import Decimal
from math import isclose

import click
import librosa
import numpy as np
from tqdm import tqdm

from get_pitch import get_pitch_parselmouth


def try_resolve_note_slur_by_matching(ph_dur, ph_num, note_dur, tol):
    if len(ph_num) > len(note_dur):
        raise ValueError("ph_num should not be longer than note_dur.")
    ph_num_cum = np.cumsum([0] + ph_num)
    word_pos = np.cumsum([sum(ph_dur[l:r]) for l, r in zip(ph_num_cum[:-1], ph_num_cum[1:])])
    note_pos = np.cumsum(note_dur)
    new_note_dur = []

    note_slur = []
    idx_word, idx_note = 0, 0
    slur = False
    while idx_word < len(word_pos) and idx_note < len(note_pos):
        if isclose(word_pos[idx_word], note_pos[idx_note], abs_tol=tol):
            note_slur.append(1 if slur else 0)
            new_note_dur.append(word_pos[idx_word])
            idx_word += 1
            idx_note += 1
            slur = False
        elif note_pos[idx_note] > word_pos[idx_word]:
            raise ValueError("Cannot resolve note_slur by matching.")
        elif note_pos[idx_note] <= word_pos[idx_word]:
            note_slur.append(1 if slur else 0)
            new_note_dur.append(note_pos[idx_note])
            idx_note += 1
            slur = True
    return np.diff(new_note_dur, prepend=Decimal("0.0")).tolist(), note_slur


def try_resolve_slur_by_slicing(ph_dur, ph_num, note_seq, note_dur, tol):
    ph_num_cum = np.cumsum([0] + ph_num)
    word_pos = np.cumsum([sum(ph_dur[l:r]) for l, r in zip(ph_num_cum[:-1], ph_num_cum[1:])])
    note_pos = np.cumsum(note_dur)
    new_note_seq = []
    new_note_dur = []

    note_slur = []
    idx_word, idx_note = 0, 0
    while idx_word < len(word_pos):
        slur = False
        if note_pos[idx_note] > word_pos[idx_word] and not isclose(
                note_pos[idx_note], word_pos[idx_word], abs_tol=tol
        ):
            new_note_seq.append(note_seq[idx_note])
            new_note_dur.append(word_pos[idx_word])
            note_slur.append(1 if slur else 0)
        else:
            while idx_note < len(note_pos) and (
                    note_pos[idx_note] < word_pos[idx_word]
                    or isclose(note_pos[idx_note], word_pos[idx_word], abs_tol=tol)
            ):
                new_note_seq.append(note_seq[idx_note])
                new_note_dur.append(note_pos[idx_note])
                note_slur.append(1 if slur else 0)
                slur = True
                idx_note += 1
            if new_note_dur[-1] < word_pos[idx_word]:
                if isclose(new_note_dur[-1], word_pos[idx_word], abs_tol=tol):
                    new_note_dur[-1] = word_pos[idx_word]
                else:
                    new_note_seq.append(note_seq[idx_note])
                    new_note_dur.append(word_pos[idx_word])
                    note_slur.append(1 if slur else 0)
        idx_word += 1
    return new_note_seq, np.diff(new_note_dur, prepend=Decimal("0.0")).tolist(), note_slur


@click.group()
def cli():
    pass


@click.command(help="Convert a transcription file to DS files")
@click.argument(
    "transcription_file",
    type=click.Path(
        dir_okay=False,
        resolve_path=True,
        path_type=pathlib.Path,
        exists=True,
        readable=True,
    ),
    metavar="TRANSCRIPTIONS",
)
@click.argument(
    "ds_folder",
    type=click.Path(file_okay=False, resolve_path=True, path_type=pathlib.Path),
    metavar="FOLDER",
)
@click.option(
    "--overwrite",
    "-f",
    is_flag=True,
    default=False,
    help="Overwrite existing DS files",
)
@click.option(
    "--tolerance",
    "-t",
    type=float,
    default=0.001,
    help="Tolerance for ph_dur/note_dur mismatch",
    metavar="FLOAT",
)
@click.option("--hop_size", "-h", type=int, default=512, help="Hop size for f0_seq", metavar="INT")
@click.option("--sample_rate", "-s", type=int, default=44100, help="Sample rate of audio", metavar="INT")
def csv2ds(transcription_file, ds_folder, overwrite, tolerance, hop_size, sample_rate):
    """Convert a transcription file to DS file"""
    wavs_folder = transcription_file.parent / "wavs"
    assert wavs_folder.is_dir(), "wavs folder not found."

    ds_folder.mkdir(parents=True, exist_ok=True)
    with open(transcription_file, "r", encoding="utf-8") as f:
        for trans_line in tqdm(csv.DictReader(f)):
            item_name = trans_line["name"]
            wav_fn = wavs_folder / f"{item_name}.wav"
            ds_fn = ds_folder / f"{item_name}.ds"
            ph_dur = list(map(Decimal, trans_line["ph_dur"].strip().split()))
            ph_num = list(map(int, trans_line["ph_num"].strip().split()))
            note_seq = trans_line["note_seq"].strip().split()
            note_dur = list(map(Decimal, trans_line["note_dur"].strip().split()))

            assert wav_fn.is_file(), f"{item_name}.wav not found."
            assert len(ph_dur) == sum(ph_num), "ph_dur and ph_num mismatch."
            assert len(note_seq) == len(note_dur), "note_seq and note_dur should have the same length."
            assert isclose(sum(ph_dur), sum(note_dur), abs_tol=tolerance), \
                f"[{item_name}] ERROR: mismatch total duration: {sum(ph_dur) - sum(note_dur)}"

            # Resolve note_slur
            if "note_slur" in trans_line and trans_line["note_slur"]:
                note_slur = list(map(int, trans_line["note_slur"].strip().split()))
            else:
                try:
                    note_dur, note_slur = try_resolve_note_slur_by_matching(
                        ph_dur, ph_num, note_dur, tolerance
                    )
                except ValueError:
                    # logging.warning(f'note_slur is not resolved by matching for {item_name}')
                    note_seq, note_dur, note_slur = try_resolve_slur_by_slicing(
                        ph_dur, ph_num, note_seq, note_dur, tolerance
                    )
            # Extract f0_seq
            wav, _ = librosa.load(wav_fn, sr=sample_rate, mono=True)
            # length = len(wav) + (win_size - hop_size) // 2 + (win_size - hop_size + 1) // 2
            # length = ceil((length - win_size) / hop_size)
            f0_timestep, f0, _ = get_pitch_parselmouth(wav, hop_size, sample_rate)
            ds_content = [
                {
                    "offset": 0.0,
                    "text": trans_line["ph_seq"],
                    "ph_seq": trans_line["ph_seq"],
                    "ph_dur": trans_line["ph_dur"],
                    "ph_num": trans_line["ph_num"],
                    "note_seq": " ".join(note_seq),
                    "note_dur": " ".join(map(str, note_dur)),
                    "note_slur": " ".join(map(str, note_slur)),
                    "f0_seq": " ".join(map("{:.1f}".format, f0)),
                    "f0_timestep": str(f0_timestep),
                }
            ]

            if not ds_fn.exists() or overwrite:
                with open(ds_fn, "w", encoding="utf-8") as f:
                    json.dump(ds_content, f, ensure_ascii=False, indent=4)


@click.command(help="Convert DS files to a transcription file")
@click.argument(
    "ds_folder",
    type=click.Path(file_okay=False, resolve_path=True, exists=True, path_type=pathlib.Path),
    metavar="FOLDER",
)
@click.argument(
    "transcription_file",
    type=click.Path(file_okay=True, dir_okay=False, resolve_path=True, path_type=pathlib.Path),
    metavar="TRANSCRIPTIONS",
)
@click.option(
    "--overwrite",
    "-f",
    is_flag=True,
    default=False,
    help="Overwrite existing transcription file",
)
def ds2csv(ds_folder, transcription_file, overwrite):
    """Convert DS files to a transcription file"""
    if not overwrite and transcription_file.is_file():
        raise FileExistsError(f"{transcription_file} already exists.")
    from glob import glob

    transcriptions = []
    for fn in tqdm(glob(str(ds_folder / "*.ds")), ncols=80):
        fp = pathlib.Path(fn)
        with open(fp, "r", encoding="utf-8") as f:
            ds = json.load(f)
            transcriptions.append(
                {
                    "name": fp.stem,
                    "ph_seq": ds[0]["ph_seq"],
                    "ph_dur": ds[0]["ph_dur"],
                    "ph_num": ds[0]["ph_num"],
                    "note_seq": ds[0]["note_seq"],
                    "note_dur": ds[0]["note_dur"],
                    # "note_slur": ds[0]["note_slur"],
                }
            )
    with open(transcription_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "name",
                "ph_seq",
                "ph_dur",
                "ph_num",
                "note_seq",
                "note_dur",
                # "note_slur",
            ],
        )
        writer.writeheader()
        writer.writerows(transcriptions)


cli.add_command(csv2ds)
cli.add_command(ds2csv)

if __name__ == "__main__":
    cli()
