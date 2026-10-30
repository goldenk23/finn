"""Render portable architecture PNGs locally with Pillow; no browser or network needed.

Run from the project root: python docs/diagrams/render_diagrams.py
The .mmd files preserve the editable Mermaid sources from ARCHITECTURE.md.
"""
from pathlib import Path
import math
import re
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
NAMES = ['architecture-overview', 'push-digest', 'alerts', 'pull-deep', 'copilot']
TITLES = ['FINN: end-to-end architecture', 'Push Digest: Refresh Live to the feed',
          'Alerts: saved data plus your rules', 'Pull Deep: Analyze to a company report',
          'Primary-Source Copilot: question to cited answer']
FONT_DIR = Path('C:/Windows/Fonts')


def font(size, bold=False):
    candidates = [FONT_DIR / ('segoeuib.ttf' if bold else 'segoeui.ttf'),
                  Path('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf' if bold
                       else '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')]
    for candidate in candidates:
        if candidate.exists():
            return ImageFont.truetype(str(candidate), size)
    return ImageFont.load_default(size=size)


BODY = font(28)
BOLD = font(29, True)
TITLE = font(42, True)
SMALL = font(24)
INK = '#142c42'
TEAL = '#087e8b'
BG = '#f4f7fb'


class Diagram:
    def __init__(self, title, width=1500, height=2600):
        self.im = Image.new('RGB', (width, height), BG)
        self.d = ImageDraw.Draw(self.im)
        self.width = width
        self.boxes = {}
        self.d.text((48, 30), title, font=TITLE, fill=INK)
        self.d.text((48, 91), 'Read from top to bottom. Arrows show requests or information flow.',
                    font=SMALL, fill='#526779')

    def wrap(self, text, width, f=BODY):
        lines = []
        for paragraph in text.replace('<br/>', '\n').split('\n'):
            line = ''
            for word in paragraph.split():
                trial = f'{line} {word}'.strip()
                if line and self.d.textlength(trial, font=f) > width:
                    lines.append(line)
                    line = word
                else:
                    line = trial
            lines.append(line)
        return lines

    def box(self, key, text, x, y, w, fill='#ffffff', outline=TEAL, min_h=100):
        lines = self.wrap(text, w-48)
        h = max(min_h, len(lines)*39+40)
        self.d.rounded_rectangle((x, y, x+w, y+h), radius=16, fill=fill,
                                 outline=outline, width=3)
        ty = y+(h-len(lines)*39)/2
        for line in lines:
            tw = self.d.textlength(line, font=BODY)
            self.d.text((x+(w-tw)/2, ty), line, font=BODY, fill=INK)
            ty += 39
        self.boxes[key] = (x, y, w, h)
        return y+h

    def arrow(self, points, label=None, label_at=None):
        self.d.line(points, fill=TEAL, width=4, joint='curve')
        x, y = points[-1]
        px, py = points[-2]
        angle = math.atan2(y-py, x-px)
        tip = [(x, y), (x-16*math.cos(angle-.45), y-16*math.sin(angle-.45)),
               (x-16*math.cos(angle+.45), y-16*math.sin(angle+.45))]
        self.d.polygon(tip, fill=TEAL)
        if label:
            lx, ly = label_at or points[len(points)//2]
            self.label(label, lx, ly)

    def label(self, text, x, y):
        tw = self.d.textlength(text, font=SMALL)
        self.d.rounded_rectangle((x-tw/2-9, y-5, x+tw/2+9, y+32),
                                 radius=5, fill=BG)
        self.d.text((x-tw/2, y-3), text, font=SMALL, fill=INK)

    def connect(self, a, b, label=None):
        ax, ay, aw, ah = self.boxes[a]
        bx, by, bw, bh = self.boxes[b]
        start, end = (ax+aw/2, ay+ah), (bx+bw/2, by)
        if start[0] == end[0]:
            pts = [start, end]
        else:
            mid = (start[1]+end[1])/2
            pts = [start, (start[0], mid), (end[0], mid), end]
        self.arrow(pts, label, ((start[0]+end[0])/2, (start[1]+end[1])/2-13))

    def save(self, name, bottom):
        self.d.text((48, bottom+24), 'FINN  |  Source: current project code  |  Details in ARCHITECTURE.md',
                    font=SMALL, fill='#526779')
        self.im.crop((0, 0, self.width, bottom+85)).save(HERE / f'{name}.png', optimize=True)


def labels(source):
    result = {}
    pattern = r'\b([A-Z][A-Z0-9_]*)\s*(?:\[\("(.*?)"\)\]|\["(.*?)"\]|\{"(.*?)"\})'
    for match in re.finditer(pattern, source):
        result[match[1]] = next(t for t in match.groups()[1:] if t is not None)
    return result


def overview():
    d = Diagram(TITLES[0], width=1600, height=3400)
    y = d.box('user', '1. You: follow a company or ask a question', 320, 150, 960,
              fill='#e5f5f5')
    y = d.box('ui', '2. Browser — Next.js + React, port 3000\nPush Digest (filtered announcements) • Alerts (rule matches)\nPull Deep (company research) • Portfolio (company watchlist)',
              180, y+55, 1240)
    d.connect('user', 'ui')
    y = d.box('api', '3. Next.js forwards /api requests to FastAPI, port 8000\nFastAPI chooses the workflow and returns structured display data',
              180, y+55, 1240)
    d.connect('ui', 'api')
    top = y+65
    workers = [('push', 'Push Digest + Alerts\npush_engine.py\nFilter announcements and evaluate rules'),
               ('pull', 'Pull Deep + Copilot\npull_engine.py\nBuild reports and answer questions'),
               ('watch', 'Portfolio operations\nAdd or remove tracked companies')]
    bottoms = []
    for i, (key, text) in enumerate(workers):
        bottoms.append(d.box(key, text, 50+i*510, top, 480, min_h=220))
        d.connect('api', key)
    panel_y = max(bottoms)+70
    specs = [
        ('collect', 'Collection + PDF reading\nscrapers/ + pdf_utils.py\nFetch data and extract relevant pages',
         'External information\nNSE / BSE (Indian stock exchanges): official announcements\nScreener.in: financial tables and document links\nYahoo Finance: price and volume (shares traded)\nDuckDuckGo: executive background'),
        ('ai', 'AI routing + cost tracking\nllm_router.py\nChoose model; log usage and estimated cost',
         'Google Gemini\nTier 1: classify announcements\nTier 2: extract evidence and answer questions\nTier 3: assemble research\nAccess: Vertex AI credentials or Gemini API key'),
        ('store', 'Local storage\nRead saved data first where applicable\nSave results for reuse',
         'SQLite: backend/finn.db\nWatchlist, prices, announcements, alert rules, reports, AI usage\n\nPDF files: backend/cache_pdfs/\nDownloaded documents reused for extraction')]
    panel_h = 80 + max(max(205, len(d.wrap(upper, 465-48))*39+40)
                       + 40 + max(310, len(d.wrap(lower, 465-48))*39+40)
                       for _, upper, lower in specs) + 28
    d.d.rounded_rectangle((35, panel_y, 1565, panel_y+panel_h), radius=20,
                          fill='#e7eef7', outline='#9eb2c7', width=2)
    d.d.text((62, panel_y+22), '4. Shared backend components — used when each workflow needs them',
             font=BOLD, fill=INK)
    for key, _ in workers:
        x, by, w, h = d.boxes[key]
        d.arrow([(x+w/2, by+h), (x+w/2, panel_y)])
    inner_bottoms = []
    for i, (key, upper, lower) in enumerate(specs):
        x = 58+i*510
        by = d.box(key, upper, x, panel_y+80, 465, min_h=205)
        inner_bottoms.append(d.box(key+'-detail', lower, x, by+40, 465, min_h=310))
        d.connect(key, key+'-detail')
    bottom = max(inner_bottoms)
    assert bottom < panel_y+panel_h, 'Shared services exceed panel'
    y = d.box('result', '5. Result returns to the requesting screen\nCards, charts, alerts, or a cited Copilot answer\nFailures return an error instead of a completed result',
              180, panel_y+panel_h+65, 1240, fill='#e5f5f5')
    d.arrow([(800, panel_y+panel_h), (800, panel_y+panel_h+65)])
    d.save(NAMES[0], y)


def linear(index, nodes):
    d = Diagram(TITLES[index])
    y = 150
    for i, (key, text) in enumerate(nodes.items()):
        bottom = d.box(key, text, 225, y, 1050,
                       fill='#e5f5f5' if i in (0, len(nodes)-1) else '#ffffff')
        if i:
            d.connect(list(nodes)[i-1], key)
        y = bottom+60
    d.save(NAMES[index], y-60)


def alerts(nodes):
    d = Diagram(TITLES[2])
    y = 150
    for key in ['A', 'B', 'C', 'D']:
        bottom = d.box(key, nodes[key], 225, y, 1050,
                       fill='#fff1d6' if key == 'D' else '#ffffff')
        if key != 'A': d.connect(chr(ord(key)-1), key)
        y = bottom+60
    bottoms = []
    for i, (key, tag) in enumerate([('E', 'Price / volume'), ('F', 'Watched category'), ('G', 'No match')]):
        bottoms.append(d.box(key, nodes[key], 35+i*490, y+45, 450, min_h=205))
        d.connect('D', key)
    _, decision_y, _, decision_h = d.boxes['D']
    for key, tag in [('E', 'Price / volume'), ('F', 'Watched category'), ('G', 'No match')]:
        x, by, w, _ = d.boxes[key]
        d.label(tag, x+w/2, (decision_y+decision_h+by)/2-13)
    d.save(NAMES[2], max(bottoms))


def research(nodes):
    d = Diagram(TITLES[3], height=3300)
    y = 150
    for key in ['A', 'B', 'C']:
        bottom = d.box(key, nodes[key], 225, y, 1050,
                       fill='#fff1d6' if key == 'C' else '#ffffff')
        if key != 'A': d.connect(chr(ord(key)-1), key)
        y = bottom+60
    cache_bottom = d.box('D', nodes['D'], 45, y+45, 650, fill='#e5f5f5')
    fresh_bottom = d.box('E', nodes['E'], 805, y+45, 650)
    d.connect('C', 'D', 'Yes: reuse saved report')
    d.connect('C', 'E', 'No, or Re-run')
    for prev, key in [('E', 'F'), ('F', 'G'), ('G', 'H')]:
        fresh_bottom = d.box(key, nodes[key], 805, fresh_bottom+60, 650)
        d.connect(prev, key)
    final_top = max(fresh_bottom, cache_bottom)+70
    bottom = d.box('I', nodes['I'], 225, final_top, 1050, fill='#e5f5f5')
    d.connect('H', 'I')
    d.arrow([(370, cache_bottom), (370, final_top-25), (550, final_top-25), (550, final_top)],
            'Saved path skips new synthesis', (370, cache_bottom+60))
    d.save(NAMES[3], bottom)


def main():
    markdown = ROOT / 'ARCHITECTURE.md'
    text = markdown.read_text(encoding='utf-8')
    blocks = re.findall(r'```mermaid\n(.*?)\n```', text, re.S)
    if blocks:
        assert len(blocks) == 5
        for name, block in zip(NAMES, blocks):
            (HERE / f'{name}.mmd').write_text(block+'\n', encoding='utf-8')
    else:
        blocks = [(HERE / f'{name}.mmd').read_text(encoding='utf-8') for name in NAMES]
    overview()
    linear(1, labels(blocks[1]))
    alerts(labels(blocks[2]))
    research(labels(blocks[3]))
    linear(4, labels(blocks[4]))
    if '```mermaid' in text:
        counter = iter(NAMES)
        def replace(_):
            name = next(counter)
            return f'![{TITLES[NAMES.index(name)]}](docs/diagrams/{name}.png)\n\n[Open full-size diagram](docs/diagrams/{name}.png) · [Editable Mermaid source](docs/diagrams/{name}.mmd)'
        text = re.sub(r'```mermaid\n.*?\n```', replace, text, flags=re.S)
        text = text.replace('Open the Markdown preview to see the Mermaid diagrams.',
                            'The diagrams are embedded PNG images, so Markdown preview does not need Mermaid support. Open any diagram at full size to read it comfortably.')
        markdown.write_text(text, encoding='utf-8')
    print('Generated five PNG diagrams and updated ARCHITECTURE.md.')


if __name__ == '__main__':
    main()
