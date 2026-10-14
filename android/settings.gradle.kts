pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}
dependencyResolutionManagement {
    repositories {
        google()
        mavenCentral()
        // yt-dlp for Android (the link downloader) is published on JitPack
        maven { url = uri("https://jitpack.io") }
    }
}
rootProject.name = "PageSnap"
include(":app")
