"""Pack Flite 2.2 lang/cmulex data: pack_flite_lts.py CMULEX OUTPUT."""

import re
import struct
import sys
from pathlib import Path


def pack(source: Path) -> bytes:
    rules = (source / "cmu_lts_rules.c").read_text()
    phones = re.findall(r'"(.*?)"', rules.split("cmu_lts_phone_table")[1].split("};")[0])
    roots = re.findall(r"(\d+), /\* [a-z] \*/", rules.split("cmu_lts_letter_index")[1])
    states = {
        name: bytes.fromhex(f"{low}{high}")
        for name, low, high in re.findall(
            r"^#define (LTS_STATE_\w+) 0x(..),0x(..)",
            (source / "cmu_lts_model.h").read_text(),
            re.MULTILINE,
        )
    }
    model = bytearray()
    for line in (source / "cmu_lts_model.c").read_text().splitlines():
        branch = re.fullmatch(r"\s*(\d+),\s*'(.)',\s*(LTS_STATE_\w+)\s*,\s*(LTS_STATE_\w+)\s*,\s*", line)
        leaf = re.fullmatch(r"\s*255\s*,\s*(\d+)\s*,\s*(?:0\s*,\s*){4}", line)
        if branch:
            feature, value, yes, no = branch.groups()
            model.extend(bytes((int(feature), ord(value))) + states[yes] + states[no])
        elif leaf:
            model.extend(bytes((255, int(leaf[1]), 0, 0, 0, 0)))
        elif re.match(r"\s*\d", line) and line.replace(" ", "") != "0,0,0,0,0,0":
            raise ValueError(f"Unrecognized Flite rule {line}")
    if len(roots) != 26 or len(model) != 153024:
        raise ValueError("Unexpected Flite letter-to-sound model layout")
    # AX0 becomes CMUdict AH0 to match the sherpa model alphabet.
    alphabet = " ".join(phones).upper().replace("AX0", "AH0").encode("ascii")
    return struct.pack("<26H", *map(int, roots)) + alphabet + b"\0" + model


if __name__ == "__main__":
    Path(sys.argv[2]).write_bytes(pack(Path(sys.argv[1])))
