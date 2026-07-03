import re

# Matches a run that starts and ends with a Cyrillic letter, allowing spaces,
# digits, and common punctuation in between so phrases like "Сервер 1, привет!"
# convert as one contiguous block instead of fragmenting at each space.
CYRILLIC_RUN = re.compile(
    r'[\u0400-\u04FF](?:[\u0400-\u04FF0-9 .,!?:;\-()\'"^]*[\u0400-\u04FF])?'
)

def convert(text: str) -> bytes:
    out = bytearray()
    pos = 0
    for m in CYRILLIC_RUN.finditer(text):
        out += text[pos:m.start()].encode('utf-8')
        out += m.group(0).encode('cp1251')
        pos = m.end()
    out += text[pos:].encode('utf-8')
    return bytes(out)

if __name__ == '__main__':
    import sys
    path_in, path_out = sys.argv[1], sys.argv[2]
    with open(path_in, 'r', encoding='utf-8') as f:
        text = f.read()
    data = convert(text)
    with open(path_out, 'wb') as f:
        f.write(data)
    print(f"Wrote {len(data)} bytes to {path_out}")