# PageSnap for Android

A real app, not a web page — and that is a requirement, not a preference. A
browser can never download an Instagram or YouTube video by itself, so the
download has to happen inside an app on the phone. Because PageSnap does that, the
whole chain works with **no server, no account, no database and no operator**:

```
link (or a file from storage)
      ↓  yt-dlp runs ON THE PHONE          (download/VideoFetcher.kt)
   video file
      ↓  decode once, sample 3×/second      (engine/FrameSampler.kt)
   160×90 grayscale frames
      ↓  the tested detector                (engine/PageDetector.kt)
   one timestamp per page of notes
      ↓  full-resolution grab, OPTION_CLOSEST (engine/PageCapturer.kt)
   screenshots → phone storage              (data/NoteStore.kt)
      ↓  PdfDocument                        (engine/NotesPdf.kt)
   a PDF saved to Downloads/PageSnap
```

## What was verified, and what was not

Being straight about this matters more than a green tick:

* **Verified, with numbers** — the detection engine. The Kotlin in
  `engine/PageDetector.kt` is a port of `../engine/pagesnap/detect.py`, and that
  Python engine passes a 15-combination ground-truth suite (see
  `../engine/tests/README.md`). The fixtures include a page written live for nine
  seconds, a mid-writing pause, a slow scroll and a phone-filmed-screen version.
  Keep the two in step if you change either.
* **Written but not compiled** — everything else in this folder. There is no
  Android SDK, JDK or Gradle in the environment where this repository was
  assembled, so the Kotlin here has never been through a compiler, let alone a
  phone. Expect to fix small things (imports, a Compose API, a permission) on the
  first build. The hard part — the algorithm and its constants — is the part that
  *is* tested.

## Building it

```bash
# Android Studio Ladybug or newer, JDK 17
cd android
gradle wrapper --gradle-version 8.7      # once, if you want ./gradlew
gradle :app:assembleDebug
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

`minSdk 24`, `targetSdk 35`. The link downloader comes from JitPack
(`com.github.yausername.youtubedl-android`), so the first build needs the network.

**This APK cannot go on the Play Store.** Play's policy forbids apps that
download video from YouTube and Instagram. Distribute it directly (an APK link,
TapApp/SideStore, or your college's own server) and say plainly what it does. For
a student audience that is usually *better* than the store: no review delays, no
30 % cut, and you can push a fix the same day.

## Things you will want next

* A foreground service already handles link downloads (`download/DownloadService.kt`),
  but the UI does not listen for its broadcasts yet — wire `ACTION_PROGRESS` and
  `ACTION_DONE` into the review screen.
* `MediaMetadataRetriever` is used for the per-page screenshot because it is one
  line and universally available. If capture turns out slow on a long lecture,
  switch to a second `MediaCodec` seek pass — the sampler already shows the
  pattern.
* OCR (ML Kit, also offline) would let a student *search* their notes later. It is
  a natural next step and does not change any of the architecture.
