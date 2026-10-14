/**
 * A small, dependency-free PDF writer.
 *
 * Each captured page is one JPEG, embedded as-is (/DCTDecode) and scaled to fill
 * the paper. No library, no CDN, so the page works from a plain file:// URL, from
 * GitHub Pages, or offline on a phone - and there is nothing to go stale.
 *
 * We never re-draw text: each PDF page *is* the screenshot, so the notes look
 * exactly like the video did and the file stays small.
 */

const A4 = [595.28, 841.89];
const LETTER = [612, 792];

/** Escape-free number formatting: PDF wants plain decimal, not 1e-7. */
const n = (v) => (Math.round(v * 100) / 100).toString();

class ByteWriter {
  constructor() {
    this.chunks = [];
    this.length = 0;
  }
  text(str) {
    const bytes = new TextEncoder().encode(str);
    this.chunks.push(bytes);
    this.length += bytes.length;
  }
  raw(bytes) {
    this.chunks.push(bytes);
    this.length += bytes.length;
  }
  get offset() {
    return this.length;
  }
  toUint8Array() {
    const out = new Uint8Array(this.length);
    let pos = 0;
    for (const c of this.chunks) { out.set(c, pos); pos += c.length; }
    return out;
  }
}

/**
 * @param pages  [{ jpeg: Uint8Array, width, height }]
 * @param options {pageSize:'a4'|'letter'|'exact', marginPt, pageNumbers, title}
 * @returns Uint8Array - a complete PDF
 */
export function buildPdf(pages, options = {}) {
  const opts = {
    pageSize: 'a4', marginPt: 22.7, pageNumbers: false, title: '',
    ...options,
  };
  if (!pages.length) throw new Error('No pages to write.');

  const w = new ByteWriter();
  const offsets = [];          // offsets[objNumber] = byte offset
  const objects = [];          // bodies, in order; object numbers are index+1

  const pageObjIds = pages.map((_, i) => 3 + i * 3);
  const kids = pageObjIds.map((id) => `${id} 0 R`).join(' ');

  objects.push({ id: 1, body: `<< /Type /Catalog /Pages 2 0 R >>` });
  objects.push({ id: 2, body: `<< /Type /Pages /Kids [${kids}] /Count ${pages.length} >>` });

  pages.forEach((page, i) => {
    const pageId = 3 + i * 3;
    const contentId = pageId + 1;
    const imageId = pageId + 2;

    const landscape = page.width > page.height;
    let pageW, pageH;
    if (opts.pageSize === 'exact') {
      pageW = page.width; pageH = page.height;
    } else {
      const [shortSide, longSide] = opts.pageSize === 'letter' ? LETTER : A4;
      pageW = landscape ? longSide : shortSide;
      pageH = landscape ? shortSide : longSide;
    }
    const margin = opts.pageSize === 'exact' ? 0 : opts.marginPt;
    const availW = pageW - 2 * margin;
    const availH = pageH - 2 * margin;
    const scale = Math.min(availW / page.width, availH / page.height);
    const drawW = page.width * scale;
    const drawH = page.height * scale;
    const x = (pageW - drawW) / 2;
    const y = (pageH - drawH) / 2;

    let content = `q\n${n(drawW)} 0 0 ${n(drawH)} ${n(x)} ${n(y)} cm\n/Im0 Do\nQ\n`;
    if (opts.pageNumbers) {
      const label = `${i + 1} / ${pages.length}`;
      content += `BT /F1 8 Tf 0.55 g ${n(pageW - margin - 14)} ${n(Math.max(10, margin * 0.5))} Td (${label}) Tj ET\n`;
    }
    const contentBytes = new TextEncoder().encode(content);

    objects.push({
      id: pageId,
      body: `<< /Type /Page /Parent 2 0 R /MediaBox [0 0 ${n(pageW)} ${n(pageH)}] `
          + `/Resources << /XObject << /Im0 ${imageId} 0 R >>`
          + (opts.pageNumbers ? ` /Font << /F1 ${3 + pages.length * 3} 0 R >>` : '')
          + ` >> /Contents ${contentId} 0 R >>`,
    });
    objects.push({
      id: contentId,
      body: null,                       // a stream, handled below
      stream: contentBytes,
      dict: `<< /Length ${contentBytes.length} >>`,
    });
    objects.push({
      id: imageId,
      body: null,
      stream: page.jpeg,
      dict: `<< /Type /XObject /Subtype /Image /Width ${page.width} /Height ${page.height} `
          + `/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode `
          + `/Length ${page.jpeg.length} >>`,
    });
  });

  if (opts.pageNumbers) {
    objects.push({ id: 3 + pages.length * 3, body: `<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>` });
  }

  objects.sort((a, b) => a.id - b.id);

  // header - a comment of high bytes tells tools this file is binary
  w.text('%PDF-1.4\n');
  w.raw(new Uint8Array([0x25, 0xE2, 0xE3, 0xCF, 0xD3, 0x0A]));

  for (const obj of objects) {
    offsets[obj.id] = w.offset;
    w.text(`${obj.id} 0 obj\n`);
    if (obj.stream) {
      w.text(`${obj.dict}\nstream\n`);
      w.raw(obj.stream);
      w.text('\nendstream\n');
    } else {
      w.text(`${obj.body}\n`);
    }
    w.text('endobj\n');
  }

  const xrefOffset = w.offset;
  const count = objects.length + 1;
  w.text(`xref\n0 ${count}\n`);
  w.text('0000000000 65535 f \n');
  for (let id = 1; id < count; id++) {
    const off = offsets[id] ?? 0;
    w.text(`${off.toString().padStart(10, '0')} 00000 n \n`);
  }
  w.text(`trailer\n<< /Size ${count} /Root 1 0 R >>\nstartxref\n${xrefOffset}\n%%EOF\n`);

  return w.toUint8Array();
}

/** Draw a canvas into JPEG bytes at the chosen quality. */
export function canvasToJpeg(canvas, quality = 0.92) {
  return new Promise((resolve) => {
    canvas.toBlob(async (blob) => {
      resolve(new Uint8Array(await blob.arrayBuffer()));
    }, 'image/jpeg', quality);
  });
}

/** A filename that survives Android, iOS, Windows and macOS. */
export function safeFilename(name, fallback = 'notes') {
  const cleaned = (name || '')
    .replace(/[^\w \-_().[\]]+/g, '')
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/^[.\-_]+|[.\-_]+$/g, '')
    .slice(0, 80);
  const stem = cleaned || fallback;
  return /\.pdf$/i.test(stem) ? stem : `${stem}.pdf`;
}
