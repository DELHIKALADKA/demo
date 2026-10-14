package app.pagesnap.engine

import kotlin.math.abs
import kotlin.math.max
import kotlin.math.min
import kotlin.math.sqrt

/**
 * Page-change detection - the heart of PageSnap. This is a line-for-line port of
 * the Python engine that is verified against ground-truth fixtures in
 * `engine/tests`; keep the two in step.
 *
 * The goal
 * --------
 * Given a video of someone teaching (slides, a scanned page, a whiteboard), we
 * want *one* screenshot per page of notes. Not one per frame, not three per
 * fade, not one per pen stroke.
 *
 * Why this is not just "scene detection"
 * --------------------------------------
 * Scene detection finds hard cuts. A lecture video has three kinds of change and
 * only the first is handled by it:
 *
 *  1. HARD CUT     the slide jumps straight to the next slide.  -> want a page.
 *  2. TRANSITION   a fade/wipe lasting 0.3-1.0s.                -> want ONE page,
 *                  captured *after* the transition, never mid-fade.
 *  3. PROGRESSIVE  the teacher keeps writing on the SAME page.  -> want ONE page,
 *                  captured when the writing finally stops.
 *
 * So a page is not "a moment where pixels changed a lot". A page is:
 *
 *      a stretch of video that held still long enough to be read.
 *
 * Everything follows from that. We look for *transitions*, then take the picture
 * at the moment the frame settles again. Writing never settles, so it never
 * fires the trigger on its own; a fade only settles once it is over, so we
 * capture the finished slide; and "has the page stopped gaining ink?" is what
 * separates a finished page from one still being written on.
 *
 * The signal
 * ----------
 * The video is sampled 3x/second as tiny 160x90 grayscale frames (on Android the
 * Y plane of the decoded frame *is* a grayscale image, so this is nearly free).
 * Neighbouring samples are compared with three cheap, complementary measures:
 *
 *   pixels   mean |a-b| over a 48x27 grid, brightness/contrast normalised away.
 *   profile  row-means + column-means as 1-D curves: "a block of text appeared".
 *   ink      per-cell structure map (6x6): "more/less stuff is written here".
 *
 *      d = 0.34*pixels + 0.33*profile + 0.33*ink
 *
 * The thresholds below were measured, not invented: see `tests/README.md` and
 * `tests/metric_experiment.py` in the repository.
 *
 * The five rules
 * --------------
 *  R1 CUT      d[i] is a local peak above the adaptive cut threshold -> a
 *              transition starts here.
 *  R2 DRIFT    the picture has drifted far from the page we last kept, is still
 *              for a *longer* window, and has stopped gaining ink -> a page was
 *              being filled in (or the camera panned) and is now done.
 *  R3 SETTLE   whichever rule fired, walk forward to the first sample where the
 *              picture has held still and capture THERE, at full resolution.
 *  R4 QUALITY  inside the still window keep the sharpest sample.
 *  R5 FOLD     a capture that a later capture merely *extends*, with nothing
 *              sudden in between, is the same page half-finished -> keep the
 *              fuller one. Hard cuts are never folded: that page really appeared.
 */
object DetectDefaults {
    const val ANALYZE_FPS: Float = 3.0f
    const val ANALYSIS_WIDTH = 160
    const val ANALYSIS_HEIGHT = 90

    const val GRID_W = 48
    const val GRID_H = 27
    const val CELLS = 6
    const val STD_FLOOR = 12.0f

    const val W_PIXEL = 0.34f
    const val W_PROFILE = 0.33f
    const val W_INK = 0.33f

    const val FOLD_MAX_LOST_INK = 0.015f
    const val FOLD_MIN_GAIN = 0.04f
}

/** One of the three ways a video can look, as far as page-finding goes. */
enum class Sensitivity { STRICT, BALANCED, EAGER }

data class DetectConfig(
    val analyzeFps: Float = DetectDefaults.ANALYZE_FPS,
    val analysisWidth: Int = DetectDefaults.ANALYSIS_WIDTH,
    val analysisHeight: Int = DetectDefaults.ANALYSIS_HEIGHT,

    /** instant change: the slide jumps to the next slide */
    val cutFloor: Double = 0.055,
    /** the page was filled in / the camera panned and stopped */
    val driftFloor: Double = 0.075,
    /** below this the picture counts as holding still */
    val settleFloor: Double = 0.048,

    val adaptiveCut: Double = 2.2,
    val adaptiveDrift: Double = 2.6,
    val adaptiveSettle: Double = 1.8,
    // Capped: on a video that is always moving an uncapped multiplier would hide
    // a real page change. A real change is always a large, sudden jump.
    val cutCeiling: Double = 0.200,
    val driftCeiling: Double = 0.220,
    val settleCeiling: Double = 0.100,

    /** after a CUT the page is instantly final */
    val settleSeconds: Double = 0.34,
    /** after a slow change wait much longer: a teacher pausing mid-sentence must
     *  not be mistaken for a finished page */
    val driftSettleSeconds: Double = 1.80,
    /** writing adds ink (+0.24..+1.5 per window); a finished page adds 0.000;
     *  a wobbling hand-held camera's noise floor is ~0.13 */
    val inkGainLimit: Float = 0.15f,

    /** debounce: no two pages closer than this */
    val minPageSeconds: Double = 1.30,
    /** below this distance two settled captures are the same page seen twice */
    val mergeDistance: Double = 0.045,
    val dropBlankPages: Boolean = true,
    /** a frame with almost no pixel variance is blank (black leader, title card) */
    val blankStd: Double = 7.0,
    val skipFirstSeconds: Double = 0.0,
    val maxPages: Int = 400,
) {
    companion object {
        fun preset(s: Sensitivity): DetectConfig = when (s) {
            Sensitivity.BALANCED -> DetectConfig()
            // fewer, high-confidence pages: use when a wrong screenshot is worse
            // than a missing one
            Sensitivity.STRICT -> DetectConfig(
                cutFloor = 0.080, driftFloor = 0.100, minPageSeconds = 2.0,
            )
            // catch subtle changes: dim scans, camcorder footage of paper
            Sensitivity.EAGER -> DetectConfig(
                cutFloor = 0.042, driftFloor = 0.058, minPageSeconds = 1.0,
            )
        }
    }
}

/**
 * A cheap fingerprint of one sampled instant of the video. All maps are flat
 * arrays for speed; nothing here is ever written to disk.
 */
class FrameSig(
    val index: Int,
    val t: Double,
    /** GRID_H x GRID_W, contrast normalised into 0..1 */
    val pixels: FloatArray,
    /** GRID_W + GRID_H row/column means */
    val profile: FloatArray,
    /** CELLS x CELLS local contrast: "how much is written here" */
    val ink: FloatArray,
    /** CELLS x CELLS local spread: "how busy is it" */
    val structure: FloatArray,
    /** variance of Laplacian: used to pick the least blurry frame */
    val sharpness: Double,
    val stddev: Double,
    val mean: Double,
) {
    val inkTotal: Float get() = ink.sum()
}

object Signatures {

    /**
     * Build a signature from a grayscale frame (the Y plane of the video).
     * `gray` is [srcW] x [srcH] bytes, row-major.
     */
    fun of(index: Int, t: Double, gray: ByteArray, srcW: Int, srcH: Int): FrameSig {
        val small = boxDownsample(gray, srcW, srcH, DetectDefaults.GRID_W, DetectDefaults.GRID_H)

        var sum = 0f
        for (v in small) sum += v
        val mean = sum / small.size
        var acc = 0f
        for (v in small) {
            val d = v - mean
            acc += d * d
        }
        val std = sqrt(acc / small.size)
        val scale = max(std, DetectDefaults.STD_FLOOR)
        val norm = FloatArray(small.size) { i ->
            val v = (small[i] - mean) / scale
            v.coerceIn(-1f, 1f)
        }

        val profile = FloatArray(DetectDefaults.GRID_W + DetectDefaults.GRID_H)
        for (y in 0 until DetectDefaults.GRID_H) {
            var row = 0f
            for (x in 0 until DetectDefaults.GRID_W) row += norm[y * DetectDefaults.GRID_W + x]
            profile[y] = row / DetectDefaults.GRID_W
        }
        for (x in 0 until DetectDefaults.GRID_W) {
            var col = 0f
            for (y in 0 until DetectDefaults.GRID_H) col += norm[y * DetectDefaults.GRID_W + x]
            profile[DetectDefaults.GRID_H + x] = col / DetectDefaults.GRID_H
        }

        val c = DetectDefaults.CELLS
        val ink = FloatArray(c * c)
        val structure = FloatArray(c * c)
        for (cy in 0 until c) {
            val y0 = cy * DetectDefaults.GRID_H / c
            val y1 = max((cy + 1) * DetectDefaults.GRID_H / c, y0 + 1)
            for (cx in 0 until c) {
                val x0 = cx * DetectDefaults.GRID_W / c
                val x1 = max((cx + 1) * DetectDefaults.GRID_W / c, x0 + 1)
                var absSum = 0f
                var sqSum = 0f
                var n = 0
                for (y in y0 until y1) {
                    for (x in x0 until x1) {
                        val v = norm[y * DetectDefaults.GRID_W + x]
                        absSum += abs(v)
                        sqSum += v * v
                        n++
                    }
                }
                val idx = cy * c + cx
                ink[idx] = absSum / max(n, 1)
                structure[idx] = sqrt(sqSum / max(n, 1))
            }
        }

        var meanGray = 0.0
        for (b in gray) meanGray += (b.toInt() and 0xFF)
        meanGray /= gray.size
        var varGray = 0.0
        for (b in gray) {
            val d = (b.toInt() and 0xFF) - meanGray
            varGray += d * d
        }
        varGray /= gray.size

        return FrameSig(
            index = index, t = t, pixels = norm, profile = profile, ink = ink,
            structure = structure,
            sharpness = laplacianVariance(gray, srcW, srcH),
            stddev = sqrt(varGray), mean = meanGray,
        )
    }

    /** Area-average any grayscale image down to outW x outH. */
    fun boxDownsample(gray: ByteArray, w: Int, h: Int, outW: Int, outH: Int): FloatArray {
        val out = FloatArray(outH * outW)
        for (oy in 0 until outH) {
            val y0 = oy * h / outH
            val y1 = max((oy + 1) * h / outH, y0 + 1)
            for (ox in 0 until outW) {
                val x0 = ox * w / outW
                val x1 = max((ox + 1) * w / outW, x0 + 1)
                var acc = 0f
                var n = 0
                for (y in y0 until y1) {
                    val row = y * w
                    for (x in x0 until x1) {
                        acc += (gray[row + x].toInt() and 0xFF)
                        n++
                    }
                }
                out[oy * outW + ox] = acc / max(n, 1)
            }
        }
        return out
    }

    private fun laplacianVariance(gray: ByteArray, w: Int, h: Int): Double {
        if (w < 3 || h < 3) return 0.0
        var sum = 0.0
        var sumSq = 0.0
        var n = 0
        for (y in 1 until h - 1) {
            for (x in 1 until w - 1) {
                val i = y * w + x
                val lap = ((gray[i - w].toInt() and 0xFF) + (gray[i + w].toInt() and 0xFF) +
                        (gray[i - 1].toInt() and 0xFF) + (gray[i + 1].toInt() and 0xFF) -
                        4 * (gray[i].toInt() and 0xFF)).toDouble()
                sum += lap
                sumSq += lap * lap
                n++
            }
        }
        if (n == 0) return 0.0
        val m = sum / n
        return sumSq / n - m * m
    }
}

/** How different are two sampled instants? 0 = identical, 1 = unrelated. */
fun distance(a: FrameSig, b: FrameSig): Double {
    var dPixel = 0.0
    for (i in a.pixels.indices) dPixel += abs(a.pixels[i] - b.pixels[i])
    dPixel /= a.pixels.size

    var dProfile = 0.0
    for (i in a.profile.indices) dProfile += abs(a.profile[i] - b.profile[i])
    dProfile /= a.profile.size

    var dStruct = 0.0
    var dInk = 0.0
    for (i in a.ink.indices) {
        dStruct += abs(a.structure[i] - b.structure[i])
        dInk += abs(a.ink[i] - b.ink[i])
    }
    dStruct /= a.ink.size
    dInk /= a.ink.size
    val dInkAll = 0.5 * dStruct + 0.5 * dInk

    val d = DetectDefaults.W_PIXEL * dPixel +
            DetectDefaults.W_PROFILE * dProfile +
            DetectDefaults.W_INK * dInkAll
    return d.coerceIn(0.0, 1.0)
}

/**
 * How much ink the page gained over the last [run] samples: the "is the teacher
 * still writing?" probe. It is a *net* measure, so a wobbling hand-held camera
 * cancels out (~0.1) while a page being filled in only ever grows (+0.24..+1.5).
 */
fun netInkGain(sigs: List<FrameSig>, i: Int, run: Int): Float {
    val j = max(0, i - run)
    return sigs[i].inkTotal - sigs[j].inkTotal
}

/**
 * Is [b] just page [a] with more written on it? Manuscripts only grow: writing
 * *adds* ink and rubs nothing out, while a page change replaces content and
 * visibly loses ink (measured: writing loses 0.0-0.3%, a page change 4-8%).
 */
fun isExtension(a: FrameSig, b: FrameSig): Boolean {
    val totalB = b.inkTotal
    if (totalB < 1e-6f) return false
    var lost = 0f
    for (i in a.ink.indices) lost += max(0f, a.ink[i] - b.ink[i])
    lost /= totalB
    val gain = (totalB - a.inkTotal) / totalB
    return lost <= DetectDefaults.FOLD_MAX_LOST_INK && gain >= DetectDefaults.FOLD_MIN_GAIN
}

/** Why a page was captured - shown to the student in the review screen. */
enum class PageReason { FIRST, CUT, DRIFT, LAST }

data class PageCandidate(
    val time: Double,
    val frameIndex: Int,
    val sharpness: Double,
    val reason: PageReason,
    val score: Double = 0.0,
    val stddev: Double = 0.0,
)

data class DetectReport(
    val pages: List<PageCandidate> = emptyList(),
    val duration: Double = 0.0,
    val sampled: Int = 0,
    val notes: List<String> = emptyList(),
    val thresholds: Map<String, Double> = emptyMap(),
)

/**
 * Detects page changes. Feed it the sampled signatures from [FrameSampler] - it
 * never touches the file itself, so it is trivially testable and cheap to run.
 */
class PageDetector(private val cfg: DetectConfig = DetectConfig()) {

    fun detect(sigsIn: List<FrameSig>): DetectReport {
        val notes = ArrayList<String>()
        val sigs = if (cfg.skipFirstSeconds > 0) sigsIn.filter { it.t >= cfg.skipFirstSeconds } else sigsIn
        if (sigs.size < 3) {
            notes += "Video too short to analyse."
            val first = sigs.firstOrNull { !isBlank(it) }
            return DetectReport(
                pages = first?.let { listOf(candidate(it, PageReason.FIRST)) } ?: emptyList(),
                sampled = sigs.size, notes = notes,
            )
        }

        val n = sigs.size
        val d = DoubleArray(n)
        for (i in 1 until n) d[i] = distance(sigs[i - 1], sigs[i])

        // Adaptive floors: a video that is never still must not flag its own
        // noise as page changes - but the caps keep real changes detectable.
        val quiet = median(d)
        val thrCut = (quiet * cfg.adaptiveCut).coerceIn(cfg.cutFloor, cfg.cutCeiling)
        val thrDrift = (quiet * cfg.adaptiveDrift).coerceIn(cfg.driftFloor, cfg.driftCeiling)
        val thrSettle = (quiet * cfg.adaptiveSettle).coerceIn(cfg.settleFloor, cfg.settleCeiling)

        val fps = cfg.analyzeFps
        val settleRun = max(1, Math.round(cfg.settleSeconds * fps).toInt())
        val driftRun = max(settleRun, Math.round(cfg.driftSettleSeconds * fps).toInt())

        val candidates = ArrayList<PageCandidate>()

        firstReadable(sigs, thrSettle, settleRun)?.let { candidates += candidate(sigs[it], PageReason.FIRST) }

        var i = 1
        while (i < n - 1 && candidates.size < cfg.maxPages) {
            val isPeak = d[i] >= d[i - 1] && d[i] >= d[i + 1]
            val cut = isPeak && d[i] >= thrCut

            var drift = false
            if (!cut && candidates.isNotEmpty()) {
                val ref = candidates.last().frameIndex
                if (i - ref >= driftRun) drift = distance(sigs[i], sigs[ref]) >= thrDrift
            }

            if (cut || drift) {
                val j = if (drift) settledAfter(sigs, i, thrSettle, driftRun, cfg.inkGainLimit)
                else settledAfter(sigs, i, thrSettle, settleRun, null)
                if (j != null) {
                    val tooSoon = candidates.isNotEmpty() &&
                            sigs[j].t - candidates.last().time < cfg.minPageSeconds
                    if (!tooSoon) {
                        val best = bestInWindow(sigs, j, thrSettle, settleRun)
                        candidates += best.copy(
                            reason = if (drift) PageReason.DRIFT else PageReason.CUT,
                            score = d[i],
                        )
                    }
                    i = j + 1
                    continue
                }
            }
            i++
        }

        // The tail: the last page of the video always deserves a screenshot.
        if (candidates.isNotEmpty()) {
            val tail = bestInWindow(sigs, n - 1, thrSettle, settleRun)
            val last = candidates.last()
            if (tail.frameIndex > last.frameIndex &&
                distance(sigs[tail.frameIndex], sigs[last.frameIndex]) > cfg.mergeDistance &&
                !isBlank(sigs[tail.frameIndex])
            ) {
                candidates += tail.copy(reason = PageReason.LAST)
            }
        }

        var pages = foldIncomplete(candidates, sigs, d, thrSettle)
        pages = merge(pages, sigs, cfg.mergeDistance)
        if (cfg.dropBlankPages) {
            val kept = pages.filter { !isBlank(sigs[it.frameIndex]) }
            if (kept.size != pages.size) notes += "Dropped ${pages.size - kept.size} blank frame(s)."
            pages = kept
        }
        return DetectReport(
            pages = pages.sortedBy { it.time },
            duration = sigs.last().t,
            sampled = sigs.size,
            notes = notes,
            thresholds = mapOf("cut" to thrCut, "drift" to thrDrift, "settle" to thrSettle, "quiet" to quiet),
        )
    }

    // -- rules ---------------------------------------------------------------

    private fun isStable(sigs: List<FrameSig>, i: Int, thr: Double, run: Int): Boolean {
        if (i + run >= sigs.size) return false
        for (k in i until i + run) if (distance(sigs[k], sigs[k + 1]) >= thr) return false
        return true
    }

    /**
     * R3: the first sample where the picture has held still. For a slow change we
     * additionally require that the page has stopped gaining ink - that is what
     * tells "finished page" apart from "still being written on", which look
     * identical sample by sample.
     */
    private fun settledAfter(
        sigs: List<FrameSig>, start: Int, thr: Double, run: Int, inkGainLimit: Float?,
    ): Int? {
        val limit = sigs.size - run - 1
        var i = start
        while (i <= limit) {
            if (isStable(sigs, i, thr, run)) {
                if (inkGainLimit == null ||
                    netInkGain(sigs, min(i + run, sigs.size - 1), run) <= inkGainLimit
                ) return i
            }
            i++
        }
        return null
    }

    private fun firstReadable(sigs: List<FrameSig>, thr: Double, run: Int): Int? {
        val limit = sigs.size - run - 1
        var i = 0
        while (i <= limit) {
            if (!isBlank(sigs[i]) && isStable(sigs, i, thr, run)) return i
            i++
        }
        return 0
    }

    /** R4: inside the still stretch, keep the sharpest sample. */
    private fun bestInWindow(
        sigs: List<FrameSig>, centre: Int, thr: Double, run: Int,
    ): PageCandidate {
        val lo = max(0, centre - run)
        val hi = min(sigs.size - 1, centre + run)
        val tol = max(thr, 0.05)
        var best = centre
        for (k in lo..hi) {
            if (distance(sigs[k], sigs[centre]) < tol && sigs[k].sharpness > sigs[best].sharpness) best = k
        }
        return candidate(sigs[best], PageReason.CUT)
    }

    /**
     * R5: drop a capture that a later capture merely extends, with nothing sudden
     * in between. That second condition separates "the teacher filled this page
     * in" from "this slide swapped for a similar-looking one".
     */
    private fun foldIncomplete(
        cands: List<PageCandidate>, sigs: List<FrameSig>, d: DoubleArray, thrSettle: Double,
    ): List<PageCandidate> {
        val out = ArrayList<PageCandidate>()
        for (c in cands) {
            if (out.isNotEmpty()) {
                val a = out.last()
                var grewSlowly = c.frameIndex > a.frameIndex
                if (grewSlowly) {
                    for (k in a.frameIndex + 1..c.frameIndex) {
                        if (d[k] >= thrSettle) {
                            grewSlowly = false
                            break
                        }
                    }
                }
                if (grewSlowly && isExtension(sigs[a.frameIndex], sigs[c.frameIndex])) {
                    out[out.size - 1] = c.copy(
                        reason = if (a.reason == PageReason.FIRST || a.reason == PageReason.LAST) a.reason else c.reason,
                    )
                    continue
                }
            }
            out += c
        }
        return out
    }

    /**
     * Collapse candidates that are the same page seen twice. Jittery footage can
     * trip the cut rule twice for one transition; comparing the *settled* frames
     * is the honest test of "was this really a different page?". Keep the later
     * one: in a lecture the later frame is the finished page.
     */
    private fun merge(cands: List<PageCandidate>, sigs: List<FrameSig>, thr: Double): List<PageCandidate> {
        val out = ArrayList<PageCandidate>()
        for (c in cands) {
            if (out.isNotEmpty() &&
                distance(sigs[out.last().frameIndex], sigs[c.frameIndex]) < thr
            ) {
                out[out.size - 1] = c.copy(reason = out.last().reason)
            } else {
                out += c
            }
        }
        return out
    }

    private fun isBlank(sig: FrameSig) = sig.stddev < cfg.blankStd
}

internal fun candidate(sig: FrameSig, reason: PageReason) = PageCandidate(
    time = sig.t, frameIndex = sig.index, sharpness = sig.sharpness,
    reason = reason, stddev = sig.stddev,
)

internal fun median(values: DoubleArray): Double {
    if (values.isEmpty()) return 0.0
    val copy = values.copyOf()
    copy.sort()
    val mid = copy.size / 2
    return if (copy.size % 2 == 1) copy[mid] else (copy[mid - 1] + copy[mid]) / 2.0
}
