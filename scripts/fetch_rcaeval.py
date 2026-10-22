"""Télécharge uniquement un sous-ensemble (ex. RE2-OB) du zip RCAEval RE2 hébergé sur Zenodo.

Le zip est « stored » (non compressé) : on lit l'index (fin du fichier), puis on récupère par
plages HTTP parallèles et reprenables. Usage :
    python -I -m scripts.fetch_rcaeval RE2-OB /chemin/sortie [workers]
Résultat : <sortie>/RE2-OB/<cas>/<fichiers>  (+ ground truth = nom du dossier <service>_<faute>)
"""
import concurrent.futures as cf
import os
import struct
import sys
import time
import urllib.request
import zlib

URL = "https://zenodo.org/records/14504481/files/RE2.zip?download=1"
CHUNK = 8 * 1024 * 1024


def get(rng: tuple[int, int], tries: int = 8) -> bytes:
    for k in range(tries):
        try:
            req = urllib.request.Request(URL, headers={"Range": f"bytes={rng[0]}-{rng[1]}"})
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
            if len(data) == rng[1] - rng[0] + 1:
                return data
        except Exception:
            pass
        time.sleep(min(2 ** k, 30))
    raise RuntimeError(f"échec plage {rng}")


def read_index() -> tuple[int, list[tuple[str, int, int, int]]]:
    req = urllib.request.Request(URL, method="HEAD")
    size = int(urllib.request.urlopen(req, timeout=60).headers["Content-Length"])
    tail = get((size - 3_000_000, size - 1))
    base = size - len(tail)
    i = tail.rfind(b"PK\x05\x06")
    _, _, _, _, _, cdsize, cdoff, _ = struct.unpack("<IHHHHIIH", tail[i:i + 22])
    cd, p, ents = tail[cdoff - base:cdoff - base + cdsize], 0, []
    while p < len(cd) and cd[p:p + 4] == b"PK\x01\x02":
        (_, _, _, _, comp, _, _, _, csz, _, nl, el, cl, _, _, _, lho) = struct.unpack("<IHHHHHHIIIHHHHHII", cd[p:p + 46])
        ents.append((cd[p + 46:p + 46 + nl].decode(), csz, lho, comp))
        p += 46 + nl + el + cl
    return size, ents


def main(prefix: str, out: str, workers: int = 24):
    size, ents = read_index()
    sel = [e for e in ents if e[0].startswith(f"RE2/{prefix}/") and not e[0].endswith("/") and ".DS_Store" not in e[0]]
    lo = min(e[2] for e in sel)
    hi = max(e[2] + 30 + 512 + 1024 + e[1] for e in sel)  # en-tête local + extra : marge
    hi = min(hi, size - 1)
    os.makedirs(out, exist_ok=True)
    blob = os.path.join(out, f"{prefix}.blob")
    plan = [(a, min(a + CHUNK - 1, hi)) for a in range(lo, hi + 1, CHUNK)]
    done_file = blob + ".done"
    done = set(open(done_file).read().split()) if os.path.exists(done_file) else set()
    mode = "r+b" if os.path.exists(blob) else "w+b"
    t0, got = time.time(), 0
    with open(blob, mode) as f, open(done_file, "a") as dl, cf.ThreadPoolExecutor(workers) as ex:
        futs = {ex.submit(get, r): r for r in plan if str(r[0]) not in done}
        for fut in cf.as_completed(futs):
            a, b = futs[fut]
            f.seek(a - lo); f.write(fut.result()); f.flush()
            dl.write(f"{a}\n"); dl.flush(); got += b - a + 1
            print(f"{got / 1e6:.0f}/{(hi - lo) / 1e6:.0f} MB  {got / (time.time() - t0) / 1e3:.0f} kB/s", flush=True)
    # extraction
    n = 0
    with open(blob, "rb") as f:
        for name, csz, lho, comp in sel:
            f.seek(lho - lo)
            h = f.read(30)
            nl, el = struct.unpack("<HH", h[26:30])
            f.seek(lho - lo + 30 + nl + el)
            dest = os.path.join(out, name.removeprefix("RE2/"))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            dec = zlib.decompressobj(-15) if comp == 8 else None  # 8 = deflate, 0 = stored
            with open(dest, "wb") as g:
                left = csz
                while left:
                    buf = f.read(min(left, 1 << 20)); left -= len(buf)
                    g.write(dec.decompress(buf) if dec else buf)
                if dec:
                    g.write(dec.flush())
            n += 1
    print(f"extrait {n} fichiers dans {out}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 24)
