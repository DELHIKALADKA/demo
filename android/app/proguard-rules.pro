# yt-dlp for Android loads its Python side by reflection.
-keep class com.yausername.** { *; }
-keep class org.python.** { *; }

# Our engine is plain Kotlin with no reflection, so the default rules are fine.
