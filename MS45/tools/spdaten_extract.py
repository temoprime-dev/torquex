#!/usr/bin/env python3
"""Извлечение стоковых образов MS45 из SP-Daten BMW (.0PA / .0DA, Intel HEX).

Формат: адрес = ext<<16 + seg<<4 + смещение (записи типа 4 и 2); запись типа 0x10 -
16 (или 4) байта, лежащие сразу ЗА последней записью данных блока. Без неё в
образе остаются дыры из FF (проверено: 64/64 калибровки в MS45/firmware
совпали байт в байт с пересборкой из .0DA).

Раскладка MS45 (доказана на дампах 456BN00S):
  .0PA, ext 0x0000, 0x00000-0x6FFFF  -> внутренняя флэш MPC (448 KiB)
  .0PA, ext 0x0200, 0x60000-0xFFFFF  -> внешняя флэш 1 MiB, программа (640 KiB)
  .0DA, ext 0x0200, 0x40000-0x5CFFF  -> внешняя флэш, калибровка (116 KiB, 0x1D000)
  внешняя флэш 0x00000-0x3FFFF в SP-Daten отсутствует.

Использование: spdaten_extract.py <каталог new_spdaten> <каталог вывода>
"""
import glob, hashlib, os, re, sys
from pathlib import Path

# Прямой запуск скриптом (``python MS45/tools/<инструмент>.py``): в этом режиме
# пакета MS45 в ``sys.path`` нет, поэтому добавляем корень репозитория. Импорты
# ниже — всегда пакетные, чтобы модуль был один, а не две его копии.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from MS45.tools.console import prepare_stdout  # noqa: E402

CAL_BASE, CAL_SIZE = 0x02040000, 0x1D000
EXT_BASE, EXT_PROG_OFF, EXT_SIZE = 0x02000000, 0x60000, 0x100000
MPC_SIZE = 0x70000


def parse_hex(path):
    mem, seg, ext, nxt = {}, 0, 0, None
    for ln in open(path, 'rb').read().decode('latin1').splitlines():
        if not ln.startswith(':'):
            continue
        b = bytes.fromhex(ln[1:].strip())
        n, a, t = b[0], (b[1] << 8) | b[2], b[3]
        d = b[4:4 + n]
        if sum(b[:5 + n]) & 0xFF:
            raise ValueError(f'{path}: bad checksum: {ln}')
        if t == 2:
            seg = ((d[0] << 8) | d[1]) << 4
        elif t == 4:
            ext = ((d[0] << 8) | d[1]) << 16
        elif t == 0:
            for i, x in enumerate(d):
                mem[ext + seg + a + i] = x
            nxt = ext + seg + a + n
        elif t == 16 and nxt is not None:
            for i, x in enumerate(d):
                mem[nxt + i] = x
    return mem


def image(mem, base, size):
    return bytes(mem.get(base + i, 0xFF) for i in range(size))


def reference(path):
    head = open(path, 'rb').read(4000).decode('latin1')
    m = re.search(r';;ZL_Referenz:\s*(\S+)', head)
    return m.group(1) if m else None


def main(src, dst):
    seen, rows = {}, []
    for p in sorted(glob.glob(os.path.join(src, '*', 'data', 'MDS45[01]', '*'))):
        ext = p.rsplit('.', 1)[-1].upper()
        if ext not in ('0PA', '0DA'):
            continue
        fam = 'MS45.0' if 'MDS450' in p else 'MS45.1'
        ref = reference(p)
        if not ref:
            continue
        mem = parse_hex(p)
        stem = os.path.splitext(os.path.basename(p))[0].upper()
        if ext == '0DA':
            outs = [('cal', f'{fam}_{ref}_{stem}_cal_116K.bin', image(mem, CAL_BASE, CAL_SIZE))]
        else:
            low = {a: v for a, v in mem.items() if a < EXT_BASE}
            hi = {a - EXT_BASE: v for a, v in mem.items() if a >= EXT_BASE}
            outs = [('program', f'{fam}_{ref}_{stem}_mpc_448K.bin', image(low, 0, MPC_SIZE)),
                    ('program', f'{fam}_{ref}_{stem}_ext_0x60000-0xFFFFF.bin',
                     image(hi, EXT_PROG_OFF, EXT_SIZE - EXT_PROG_OFF))]
        for kind, name, data in outs:
            h = hashlib.sha256(data).hexdigest()
            if (kind, h) in seen:
                continue
            seen[(kind, h)] = name
            os.makedirs(os.path.join(dst, kind), exist_ok=True)
            open(os.path.join(dst, kind, name), 'wb').write(data)
            rows.append((h, kind + '/' + name))
    rows.sort(key=lambda r: r[1])
    with open(os.path.join(dst, 'SHA256SUMS.txt'), 'w', encoding='utf-8',
              newline='\n') as handle:
        handle.write(''.join(f'{h}  {n}\n' for h, n in rows))
    print(len(rows), 'files')


if __name__ == '__main__':
    prepare_stdout()
    if len(sys.argv) != 3:
        print('использование: python MS45/tools/spdaten_extract.py <SP-Daten> <куда>',
              file=sys.stderr)
        raise SystemExit(2)
    main(sys.argv[1], sys.argv[2])
