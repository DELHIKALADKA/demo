/**
 * Links and timecodes: everything the page can decide on its own.
 *
 * Nothing in this file touches the network or the DOM, so every rule below can be
 * tested straight from Node (see test/run.mjs) instead of by hoping.
 *
 * Two jobs:
 *
 *   1. Turn "1:23.456" or "01:23:45.678" into a number of seconds, and back.
 *   2. Look at a pasted link and say what it is - YouTube video, Short, community
 *      post, Instagram photo post or Reel, Facebook album - and whether *this* page
 *      could ever fetch it (it cannot: a static page is blocked by CORS), so the
 *      visitor gets the truth plus a way forward instead of a spinning box.
 *
 * What it deliberately does NOT do: pretend. A YouTube video is frames, not a folder
 * of photographs; a photo post needs a server that can talk to the platform; and a
 * private or DRM-protected post is not fetchable by anyone who is not the owner.
 */

/** Hosts that mean "the same platform", including the short and mobile ones. */
const YOUTUBE_HOSTS = new Set([
  'youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com',
  'youtu.be', 'www.youtu.be', 'youtube-nocookie.com', 'www.youtube-nocookie.com',
]);
const INSTAGRAM_HOSTS = new Set([
  'instagram.com', 'www.instagram.com', 'm.instagram.com', 'instagr.am', 'www.instagr.am',
]);
const FACEBOOK_HOSTS = new Set([
  'facebook.com', 'www.facebook.com', 'm.facebook.com', 'web.facebook.com', 'fb.com',
  'fb.watch', 'www.fb.watch', 'fb.me', 'www.fb.me',
]);

/** Query keys that only exist to tag a campaign, not to identify the content. */
const TRACKING_KEYS = [
  'si', 'igsh', 'igshid', 'fbclid', 'utm_source', 'utm_medium', 'utm_campaign',
  'utm_term', 'utm_content', 'utm_id', 'gclid', '_openstat', 'ref', 'refsrc',
  'mibextid', 'app', 'feature', 'share_url',
  // "t=42s" is where somebody was watching, not what they were watching, so the
  // normalised address leaves it out.
  't',
];

/**
 * @param {string} raw
 * @returns {{ok:true,url:URL}|{ok:false,error:string}}
 */
export function parseLink(raw) {
  const text = String(raw == null ? '' : raw).trim();
  if (!text) return { ok: false, error: 'Paste a link first.' };
  if (/\s/.test(text)) {
    return { ok: false, error: 'That has spaces in it, so it is not one link. Paste a single URL.' };
  }
  // People paste links without the scheme. Add https:// only when it looks like a
  // bare host, so "javascript:alert(1)" never turns into something we would follow.
  const withScheme = /^[a-z][a-z0-9+.-]*:/i.test(text) ? text : `https://${text}`;
  let url;
  try {
    url = new URL(withScheme);
  } catch (_) {
    return { ok: false, error: 'That is not a valid URL.' };
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') {
    return { ok: false, error: 'Only http and https links can be opened.' };
  }
  if (url.username || url.password) {
    // A URL with credentials in it is not something to fetch, and it suggests the
    // visitor was handed a link that leaks a password.
    return { ok: false, error: 'That link carries a username and password inside it, so it will not be opened.' };
  }
  if (url.port && url.port !== '80' && url.port !== '443') {
    return { ok: false, error: 'That link points at a specific port, which is not something a video or photo post uses.' };
  }
  if (!url.hostname.includes('.')) {
    return { ok: false, error: 'That link has no real domain in it.' };
  }
  return { ok: true, url };
}

/** Drop campaign junk and keep the parameters that identify the content. */
function tidy(url, keepKeys) {
  const params = new URLSearchParams(url.search);
  for (const key of [...params.keys()]) {
    if (TRACKING_KEYS.includes(key) && !keepKeys.includes(key)) params.delete(key);
  }
  url.search = params.toString();
  url.hash = '';
  return url.toString();
}

/**
 * What kind of thing is this, and can this page fetch it?
 *
 * `extractable` is about *this static page*, not about the platform: a browser
 * cannot read instagram.com or youtube.com cross-origin, and no amount of JavaScript
 * changes that. The server prototype can, for video. Photo posts need more than
 * yt-dlp provides, and saying otherwise would be a lie.
 *
 * @returns {{
 *   ok: boolean, platform: string, kind: string, label: string,
 *   normalized: string, contentId: string,
 *   yields: 'frames'|'photos'|'unknown',
 *   extractable: boolean, limit: string, alternative: string,
 *   error?: string
 * }}
 */
export function classifyUrl(raw) {
  const parsed = parseLink(raw);
  if (!parsed.ok) {
    return {
      ok: false, platform: 'unknown', kind: 'invalid', label: 'Not a usable link',
      normalized: '', contentId: '', yields: 'unknown', extractable: false,
      error: parsed.error, limit: '', alternative: 'Upload the video or the photos from this device instead.',
    };
  }
  const url = parsed.url;
  const host = url.hostname.toLowerCase();
  const path = url.pathname.replace(/\/+$/, '') || '/';
  const parts = path.split('/').filter(Boolean);

  // ── YouTube ───────────────────────────────────────────────────────────────
  if (YOUTUBE_HOSTS.has(host)) {
    // youtu.be/<id>
    if (host.endsWith('youtu.be')) {
      const id = parts[0] || '';
      if (!id) return youTubeBad(url);
      return videoLink(url, `https://www.youtube.com/watch?v=${id}`, id,
        'a YouTube video');
    }
    if (parts[0] === 'watch') {
      const id = url.searchParams.get('v') || '';
      if (!id) return youTubeBad(url);
      // Keep what identifies the content (the video, and a playlist if it came from
      // one). A "t=42s" start offset is where somebody was watching, not what they
      // were watching, so it does not belong in the normalised address.
      const kept = tidy(new URL(url.toString()), ['v', 'list']);
      return videoLink(url, kept, id, 'a YouTube video');
    }
    if (parts[0] === 'shorts' && parts[1]) {
      return videoLink(url, `https://www.youtube.com/shorts/${parts[1]}`, parts[1],
        'a YouTube Short');
    }
    if (parts[0] === 'live' && parts[1]) {
      return videoLink(url, `https://www.youtube.com/live/${parts[1]}`, parts[1],
        'a YouTube live stream');
    }
    if (parts[0] === 'embed' && parts[1]) {
      return videoLink(url, `https://www.youtube.com/watch?v=${parts[1]}`, parts[1],
        'a YouTube video');
    }
    if (parts[0] === 'post' && parts[1]) {
      return {
        ok: true, platform: 'youtube', kind: 'community_post',
        label: 'a YouTube community post',
        normalized: `https://www.youtube.com/post/${parts[1]}`, contentId: parts[1],
        yields: 'photos',
        extractable: false,
        limit: 'A community post is a post, not a video. Its pictures are served by '
             + 'YouTube to a signed-in viewer, and a page like this one has no way to '
             + 'ask for them.',
        alternative: 'Screenshot the images you want, or save them from the post and '
                   + 'upload them here to put them in a PDF.',
      };
    }
    if (parts[0] === 'playlist' || parts[0] === 'results' || parts[0] === 'channel'
        || parts[0].startsWith('@') || parts[0] === 'c' || parts[0] === 'user') {
      return {
        ok: true, platform: 'youtube', kind: 'listing',
        label: 'a YouTube channel or list', normalized: url.toString(), contentId: '',
        yields: 'unknown', extractable: false,
        limit: 'That is a list of videos, not one video, so there is no single thing to open.',
        alternative: 'Open the one video you want and paste that link, or upload the video file.',
      };
    }
    return youTubeBad(url);
  }

  // ── Instagram ─────────────────────────────────────────────────────────────
  if (INSTAGRAM_HOSTS.has(host)) {
    const kindWord = parts[0];
    if (kindWord === 'reel' || kindWord === 'reels' || kindWord === 'tv') {
      return {
        ok: true, platform: 'instagram', kind: 'reel',
        label: `an Instagram ${kindWord === 'tv' ? 'IGTV video' : 'Reel'}`,
        normalized: `https://www.instagram.com/${kindWord}/${parts[1] || ''}`,
        contentId: parts[1] || '', yields: 'frames', extractable: false,
        limit: 'Instagram serves Reels only to a signed-in, non-automated viewer, and '
             + 'the video URL it hands out expires within minutes. A page like this one '
             + 'cannot read it, and pretending otherwise would just produce an error.',
        alternative: 'Download the Reel with Instagram\'s own "Save" (when the author '
                   + 'allows it), then upload the file here - you can then capture the '
                   + 'exact frames you want.',
      };
    }
    if (kindWord === 'p' || kindWord === 'photo') {
      return {
        ok: true, platform: 'instagram', kind: 'photo_post',
        label: 'an Instagram photo post or carousel',
        normalized: `https://www.instagram.com/p/${parts[1] || ''}`,
        contentId: parts[1] || '', yields: 'photos', extractable: false,
        limit: 'A carousel\'s pictures are handed to a signed-in viewer one at a time, '
             + 'and Instagram blocks automated readers. Fetching them would mean '
             + 'defeating that protection, which this project does not do.',
        alternative: 'Save each picture from the post (⋮ → Save, or a screenshot of the '
                   + 'full-size view) and upload them here - the PDF box will put them '
                   + 'in order.',
      };
    }
    return {
      ok: true, platform: 'instagram', kind: 'profile',
      label: 'an Instagram profile', normalized: url.toString(), contentId: '',
      yields: 'unknown', extractable: false,
      limit: 'A profile is a list of posts, not one post.',
      alternative: 'Open the post or Reel you want and paste that link instead.',
    };
  }

  // ── Facebook ──────────────────────────────────────────────────────────────
  if (FACEBOOK_HOSTS.has(host)) {
    if (host.endsWith('fb.watch')) {
      return {
        ok: true, platform: 'facebook', kind: 'short_link', label: 'a Facebook video',
        normalized: url.toString(), contentId: parts[0] || '', yields: 'frames',
        extractable: false,
        limit: 'A fb.watch link redirects to the real video, and the redirect cannot be '
             + 'followed from a page like this one.',
        alternative: 'Open the link, copy the full address it lands on, and paste that; '
                   + 'or download the video and upload the file here.',
      };
    }
    if (parts[0] === 'reel' && parts[1]) {
      return {
        ok: true, platform: 'facebook', kind: 'reel', label: 'a Facebook Reel',
        normalized: `https://www.facebook.com/reel/${parts[1]}`, contentId: parts[1],
        yields: 'frames', extractable: false,
        limit: 'Facebook Reels are served to signed-in viewers, with URLs that expire.',
        alternative: 'Save the video from Facebook if the author allows it, then upload '
                   + 'the file here to capture frames.',
      };
    }
    if (parts[0] === 'watch' || parts[0] === 'video' || parts[0] === 'videos'
        || parts[0] === 'reel') {
      const id = url.searchParams.get('v') || parts[1] || '';
      return {
        ok: true, platform: 'facebook', kind: 'video', label: 'a Facebook video',
        normalized: id ? `https://www.facebook.com/watch/?v=${id}` : url.toString(),
        contentId: id, yields: 'frames', extractable: false,
        limit: 'Facebook videos are served to signed-in viewers, and most of them are '
             + 'not public at all.',
        alternative: 'If the video is yours, download it and upload the file here - '
                   + 'frame capture then works entirely on your device.',
      };
    }
    if (parts[0] === 'photo' || parts[0] === 'photos' || parts[0] === 'media'
        || parts.includes('photos') || parts.includes('photo')) {
      return {
        ok: true, platform: 'facebook', kind: 'photo_post',
        label: 'a Facebook photo post or album', normalized: url.toString(),
        contentId: url.searchParams.get('fbid') || '', yields: 'photos',
        extractable: false,
        limit: 'Album photos are shown to whoever is allowed to see the post; a page '
             + 'like this one is not that viewer.',
        alternative: 'Save the pictures you need from the album and upload them here.',
      };
    }
    return {
      ok: true, platform: 'facebook', kind: 'page', label: 'a Facebook page or profile',
      normalized: url.toString(), contentId: '', yields: 'unknown', extractable: false,
      limit: 'That link is a profile or page, not one video or one post.',
      alternative: 'Open the single video or post and paste that link instead.',
    };
  }

  // ── anything else ─────────────────────────────────────────────────────────
  const last = parts[parts.length - 1] || '';
  if (/\.(mp4|m4v|mov|webm|mkv|ogv|avi)$/i.test(last)) {
    return {
      ok: true, platform: 'direct', kind: 'video_file', label: 'a video file',
      normalized: url.toString(), contentId: last, yields: 'frames',
      extractable: false,
      limit: 'A page served from a static host cannot read a file from another site '
           + '(the browser blocks it), so this page cannot fetch it for you.',
      alternative: 'Download the file first, then upload it here - everything then runs '
                 + 'on your own device, with no network involved at all.',
    };
  }
  if (/\.(jpe?g|png|webp|gif|avif|heic)$/i.test(last)) {
    return {
      ok: true, platform: 'direct', kind: 'image_file', label: 'an image file',
      normalized: url.toString(), contentId: last, yields: 'photos',
      extractable: false,
      limit: 'The same cross-origin rule applies: this page cannot pull a picture off '
           + 'another site.',
      alternative: 'Save the picture, then upload it here.',
    };
  }
  return {
    ok: true, platform: 'web', kind: 'web_page', label: 'a web page',
    normalized: url.toString(), contentId: '', yields: 'unknown', extractable: false,
    limit: 'That is an ordinary web page. A page cannot be turned into a video or a set '
         + 'of photographs.',
    alternative: 'If there is a video on it, download that video and upload the file here.',
  };
}

function videoLink(url, normalized, id, label) {
  return {
    ok: true, platform: 'youtube', kind: 'video', label,
    normalized, contentId: id, yields: 'frames', extractable: false,
    limit: 'A video gives you frames, not a set of post photographs - that is what this '
         + 'page is for. Reading the video itself off YouTube needs a server that talks '
         + 'to YouTube (the app\'s own server does that, using yt-dlp, and only for '
         + 'videos that are not private or DRM-protected).',
    alternative: 'Either run the local app\'s server and paste this link there, or '
               + 'download the video and upload the file here to capture its frames.',
  };
}

function youTubeBad(url) {
  return {
    ok: true, platform: 'youtube', kind: 'not_content', label: 'a YouTube link with no video in it',
    normalized: url.toString(), contentId: '', yields: 'unknown', extractable: false,
    limit: 'There is no video or post in that address.',
    alternative: 'Copy the link from the video itself (Share → Copy link).',
  };
}

/**
 * "1:23.456", "01:23:45.678", "83.5", "1:23" -> seconds.
 *
 * The requested format is HH:MM.mmm; MM:SS and plain seconds are accepted because
 * they are what people actually type. Anything else is refused with the reason, so
 * the box can say what is wrong rather than silently seeking to 0.
 *
 * @returns {{ok:true,seconds:number,text:string}|{ok:false,error:string}}
 */
export function parseTimecode(raw) {
  const text = String(raw == null ? '' : raw).trim();
  if (!text) return { ok: false, error: 'Type a time first, for example 1:23.500.' };
  const normalised = text.replace(',', '.');            // 1:23,500 is a decimal comma
  if (!/^[0-9:.]+$/.test(normalised)) {
    return { ok: false, error: 'Use digits and colons only, for example 1:23.500.' };
  }
  const pieces = normalised.split(':');
  if (pieces.length > 3) {
    return { ok: false, error: 'Too many colons. Use H:MM.mmm, MM:SS.mmm or seconds.' };
  }
  const seconds = [];
  for (const piece of pieces) {
    if (piece === '') return { ok: false, error: 'There is an empty part between two colons.' };
    const value = Number(piece);
    if (!isFinite(value)) return { ok: false, error: `"${piece}" is not a number.` };
    seconds.push(value);
  }
  let total;
  if (seconds.length === 1) {
    total = seconds[0];                                  // plain seconds
  } else if (seconds.length === 2) {
    total = seconds[0] * 60 + seconds[1];                // MM:SS
  } else {
    total = seconds[0] * 3600 + seconds[1] * 60 + seconds[2];
  }
  // The minutes/seconds are allowed to be fractional in the last slot only; a middle
  // slot of 75 makes the intent unclear, so refuse rather than guess.
  for (let i = 0; i < seconds.length - 1; i++) {
    if (!Number.isInteger(seconds[i]) || seconds[i] < 0) {
      return { ok: false, error: 'Hours and minutes must be whole numbers.' };
    }
  }
  if (seconds.length === 3 && seconds[1] > 59) {
    return { ok: false, error: 'The minutes part cannot be more than 59.' };
  }
  if (seconds.length >= 2 && seconds[seconds.length - 1] >= 60) {
    return { ok: false, error: 'The seconds part cannot be 60 or more.' };
  }
  if (!isFinite(total) || total < 0) {
    return { ok: false, error: 'That time is not valid.' };
  }
  return { ok: true, seconds: total, text: formatTimecode(total) };
}

/** Seconds -> "HH:MM.mmm" (or "MM:SS.mmm" under an hour, which is easier to read). */
export function formatTimecode(seconds, decimals = 3) {
  const value = Number(seconds);
  if (!isFinite(value) || value < 0) return '0:00.000';
  const hours = Math.floor(value / 3600);
  const minutes = Math.floor((value % 3600) / 60);
  const rest = value - hours * 3600 - minutes * 60;
  const width = 2 + (decimals > 0 ? 1 + decimals : 0);
  const tail = rest.toFixed(decimals).padStart(width, '0');
  return hours > 0
    ? `${hours}:${String(minutes).padStart(2, '0')}:${tail}`
    : `${minutes}:${tail}`;
}

/**
 * Is this time inside the video?
 * @returns {{ok:true,seconds:number}|{ok:false,error:string}}
 */
export function checkTimecode(raw, durationSeconds) {
  const parsed = parseTimecode(raw);
  if (!parsed.ok) return parsed;
  const duration = Number(durationSeconds);
  if (!isFinite(duration) || duration <= 0) {
    return { ok: false, error: 'The video\'s length could not be read yet, so the time cannot be checked.' };
  }
  // Browsers can only seek to a frame that exists: at exactly the duration there is
  // nothing left to show, so the limit is the last frame before it.
  const limit = Math.max(0, duration - 0.001);
  if (parsed.seconds > limit) {
    return {
      ok: false,
      error: `That is past the end of the video — it runs ${duration.toFixed(2)}s `
           + `(${formatTimecode(duration)}).`,
    };
  }
  return { ok: true, seconds: parsed.seconds };
}

/** "1280 × 720" */
export function describeResolution(width, height) {
  const w = Math.round(Number(width) || 0);
  const h = Math.round(Number(height) || 0);
  if (!w || !h) return 'size unknown';
  return `${w} × ${h}`;
}

/**
 * Say plainly when the source is small, instead of calling every frame "original
 * quality". 720p of a written page is not enough to read small handwriting when it
 * is printed, and the person choosing that video should know before the PDF exists.
 */
export function qualityNote(width, height) {
  const w = Math.round(Number(width) || 0);
  const h = Math.round(Number(height) || 0);
  if (!w || !h) return { level: 'unknown', text: 'Resolution could not be read.' };
  const pixels = w * h;
  if (pixels >= 1920 * 1080) {
    return { level: 'high', text: 'Full detail is kept: the PDF pages use this image as it is.' };
  }
  if (pixels >= 1280 * 720) {
    return { level: 'medium', text: 'Moderate detail — fine for typing, small for handwriting.' };
  }
  return {
    level: 'low',
    text: 'The source itself is small, so the PDF cannot be sharper than this. '
        + 'Re-record at a higher resolution for small text.',
  };
}

export function humanBytes(bytes) {
  const value = Number(bytes) || 0;
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(2)} MB`;
}
