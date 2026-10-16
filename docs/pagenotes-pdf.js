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
/**
 * @param pages  [{ jpeg, width, height }]                    - JPEG, embedded as it is
 *               [{ rgbDeflated, width, height }]             - lossless (see encodeLossless)
 *               [{ rgbDeflated, alphaDeflated, width, height }] - lossless with transparency
 * @param options {pageSize:'a4'|'letter'|'exact', marginPt, pageNumbers, title,
 *                 orientation:'auto'|'portrait'|'landscape', fit:'contain'|'stretch'|'actual'}
 * @returns Uint8Array - a complete PDF
 */
export function buildPdf(pages, options = {}) {
  const opts = {
    pageSize: 'a4', marginPt: 22.7, pageNumbers: false, title: '',
    orientation: 'auto', fit: 'contain', ...options,
  };
  if (!pages.length) throw new Error('No pages to write.');
  if (!['auto', 'portrait', 'landscape'].includes(opts.orientation)) {
    throw new Error(`Unknown orientation "${opts.orientation}".`);
  }
  if (!['contain', 'stretch', 'actual'].includes(opts.fit)) {
    throw new Error(`Unknown fit mode "${opts.fit}".`);
  }

  const w = new ByteWriter();
  const offsets = [];          // offsets[objNumber] = byte offset
  const objects = [];          // bodies, in order; object numbers are index+1

  // Object numbers are handed out as we go, because a lossless image with
  // transparency needs one more object (the /SMask) than a JPEG does.
  let nextId = 3;
  // The page-number font takes an object number before the pages do, because each
  // page's resource dictionary has to point at it.
  const fontId = opts.pageNumbers ? nextId++ : null;
  const laid = pages.map((page) => {
    if (!page.width || !page.height) {
      throw new Error('A page has no size, so the PDF cannot be laid out.');
    }
    const landscape = opts.orientation === 'auto'
      ? page.width > page.height
      : opts.orientation === 'landscape';
    let pageW, pageH, margin;
    if (opts.pageSize === 'exact') {
      // "Image size": the page *is* the image, at one PDF point per pixel. Nothing is
      // rescaled and nothing is cropped, so the source pixels are exactly what prints.
      pageW = page.width; pageH = page.height; margin = 0;
    } else {
      const [shortSide, longSide] = opts.pageSize === 'letter' ? LETTER : A4;
      pageW = landscape ? longSide : shortSide;
      pageH = landscape ? shortSide : longSide;
      margin = opts.marginPt;
    }
    const availW = pageW - 2 * margin;
    const availH = pageH - 2 * margin;
    let drawW, drawH;
    if (opts.fit === 'stretch') {
      drawW = availW; drawH = availH;                  // fills the paper, aspect changes
    } else if (opts.fit === 'actual') {
      drawW = page.width; drawH = page.height;         // 1 pixel -> 1 point, never enlarged
      // If the paper is smaller than the picture, grow the page instead of shrinking
      // the picture: losing detail is the one thing this mode exists to avoid.
      const needW = drawW + 2 * margin;
      const needH = drawH + 2 * margin;
      if (needW > pageW || needH > pageH) { pageW = Math.max(pageW, needW); pageH = Math.max(pageH, needH); }
    } else {
      const scale = Math.min(availW / page.width, availH / page.height);
      drawW = page.width * scale;
      drawH = page.height * scale;
    }
    const pageId = nextId++;
    const contentId = nextId++;
    const imageId = nextId++;
    const alphaId = page.alphaDeflated ? nextId++ : null;
    return {
      page, pageId, contentId, imageId, alphaId,
      pageW, pageH, margin,
      x: (pageW - drawW) / 2, y: (pageH - drawH) / 2, drawW, drawH,
    };
  });

  const kids = laid.map((entry) => `${entry.pageId} 0 R`).join(' ');

  objects.push({ id: 1, body: `<< /Type /Catalog /Pages 2 0 R >>` });
  objects.push({ id: 2, body: `<< /Type /Pages /Kids [${kids}] /Count ${laid.length} >>` });

  laid.forEach((entry, i) => {
    const { page } = entry;
    let content = `q\n${n(entry.drawW)} 0 0 ${n(entry.drawH)} ${n(entry.x)} ${n(entry.y)} cm\n/Im0 Do\nQ\n`;
    if (opts.pageNumbers) {
      const label = `${i + 1} / ${laid.length}`;
      content += `BT /F1 8 Tf 0.55 g ${n(entry.pageW - entry.margin - 14)} `
              + `${n(Math.max(10, entry.margin * 0.5))} Td (${label}) Tj ET\n`;
    }
    const contentBytes = new TextEncoder().encode(content);

    objects.push({
      id: entry.pageId,
      body: `<< /Type /Page /Parent 2 0 R /MediaBox [0 0 ${n(entry.pageW)} ${n(entry.pageH)}] `
          + `/Resources << /XObject << /Im0 ${entry.imageId} 0 R >>`
          + (opts.pageNumbers ? ` /Font << /F1 ${fontId} 0 R >>` : '')
          + ` >> /Contents ${entry.contentId} 0 R >>`,
    });
    objects.push({
      id: entry.contentId,
      body: null,                       // a stream, handled below
      stream: contentBytes,
      dict: `<< /Length ${contentBytes.length} >>`,
    });

    // Either the JPEG the camera/canvas produced, byte for byte, or raw samples run
    // through deflate - which is how transparency survives into the PDF.
    const stream = page.jpeg || page.rgbDeflated;
    const filter = page.jpeg ? '/DCTDecode' : '/FlateDecode';
    const colour = '/DeviceRGB';
    objects.push({
      id: entry.imageId,
      body: null,
      stream,
      dict: `<< /Type /XObject /Subtype /Image /Width ${page.width} /Height ${page.height} `
          + `/ColorSpace ${colour} /BitsPerComponent 8 /Filter ${filter} `
          + (entry.alphaId ? `/SMask ${entry.alphaId} 0 R ` : '')
          + `/Length ${stream.length} >>`,
    });
    if (entry.alphaId) {
      objects.push({
        id: entry.alphaId,
        body: null,
        stream: page.alphaDeflated,
        dict: `<< /Type /XObject /Subtype /Image /Width ${page.width} /Height ${page.height} `
            + `/ColorSpace /DeviceGray /BitsPerComponent 8 /Filter /FlateDecode `
            + `/Length ${page.alphaDeflated.length} >>`,
      });
    }
  });

  if (opts.pageNumbers) {
    objects.push({ id: fontId, body: `<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>` });
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

/**
 * Run bytes through zlib, which is what PDF's /FlateDecode expects.
 *
 * CompressionStream is in every browser that can run this app at all (Chrome 80+,
 * Safari 16.4+, Firefox 113+). Where it is missing we say so rather than writing a
 * PDF that claims to be deflated and is not - a corrupt file is worse than a message.
 */
export async function deflateBytes(bytes) {
  if (typeof CompressionStream === 'undefined') {
    throw new Error('This browser cannot compress images losslessly '
      + '(no CompressionStream). Use the JPEG quality setting instead.');
  }
  const stream = new Blob([bytes]).stream().pipeThrough(new CompressionStream('deflate'));
  return new Uint8Array(await new Response(stream).arrayBuffer());
}

/**
 * A canvas's pixels, ready for buildPdf - losslessly.
 *
 * Why bother: a screenshot of a page with a transparent background, or a PNG the
 * visitor uploaded, is destroyed by turning it into JPEG. Here the RGB samples are
 * deflated as they are (no quantisation, no ringing) and the alpha rides along as a
 * PDF /SMask, so a transparent image stays transparent. An image with no transparency
 * skips the mask, which keeps the file smaller.
 *
 * @param {Uint8Array} rgba  width * height * 4 bytes, as ImageData gives them
 * @returns {Promise<{rgbDeflated:Uint8Array, alphaDeflated:Uint8Array|undefined,
 *                    width:number, height:number}>}
 */
export async function encodeLossless(rgba, width, height) {
  const w = Math.round(Number(width));
  const h = Math.round(Number(height));
  if (!w || !h) throw new Error('The image has no size, so it cannot be written into a PDF.');
  if (rgba.length < w * h * 4) {
    throw new Error('The image data is shorter than its own width and height say it should be.');
  }
  const rgb = new Uint8Array(w * h * 3);
  let opaque = true;
  for (let i = 0, j = 0; i < w * h; i++) {
    const p = i * 4;
    rgb[j++] = rgba[p];
    rgb[j++] = rgba[p + 1];
    rgb[j++] = rgba[p + 2];
    if (rgba[p + 3] !== 255) opaque = false;
  }
  const out = { rgbDeflated: await deflateBytes(rgb), alphaDeflated: undefined, width: w, height: h };
  if (!opaque) {
    const alpha = new Uint8Array(w * h);
    for (let i = 0; i < w * h; i++) alpha[i] = rgba[i * 4 + 3];
    out.alphaDeflated = await deflateBytes(alpha);
  }
  return out;
}

/** Draw a canvas into JPEG bytes at the chosen quality. */
export function canvasToJpeg(canvas, quality = 0.92) {
  return new Promise((resolve, reject) => {
    canvas.toBlob((blob) => {
      // toBlob hands back null when the browser cannot encode - on a phone that is
      // usually memory. Say that, instead of failing later with "null is not an object".
      if (!blob) {
        reject(new Error('The browser ran out of room to save that screenshot. '
          + 'Close some tabs and try again, or pick a smaller picture quality.'));
        return;
      }
      blob.arrayBuffer()
        .then((buf) => resolve(new Uint8Array(buf)))
        .catch((err) => reject(err));
    }, 'image/jpeg', quality);
  });
}

/**
 * A filename that survives Android, iOS, Windows and macOS.
 *
 * Letters and digits are kept in *any* script - a title typed in Hindi or Tamil must
 * not silently become "notes.pdf" - while the characters no filesystem accepts
 * (slashes, colons, control codes) are replaced or dropped. The extension is always
 * exactly one ".pdf".
 */
export function safeFilename(name, fallback = 'notes') {
  const cleaned = (name || '')
    .normalize('NFC')
    .replace(/[\u0000-\u001f\u007f]/g, '')             // control codes, newlines
    .replace(/[\\/:*?"<>|]+/g, '-')                    // illegal on Windows, separators elsewhere
    // Letters, numbers and *combining marks* in every script: the marks are what carry
    // the vowels in Hindi and Tamil, so dropping them turns "अध्याय" into "अधयय".
    // The zero-width joiners are kept for the same reason.
    .replace(/[^\p{L}\p{M}\p{N}\u200c\u200d \-_().[\]]+/gu, '')
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/^[.\-_]+|[.\-_]+$/g, '')
    .slice(0, 80)
    .trim();
  const stem = cleaned || fallback;
  const named = /\.pdf$/i.test(stem) ? stem : `${stem}.pdf`;
  // Windows will not create a file whose name is CON, PRN, COM1 ... whatever the
  // extension, so the download would silently fail there. Add a harmless prefix.
  return /^(con|prn|aux|nul|com[0-9]|lpt[0-9])(\..*)?$/i.test(named) ? `_${named}` : named;
}
