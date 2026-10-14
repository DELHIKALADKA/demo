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
        mavenCentral()   // the link downloader (yt-dlp for Android) lives here too
    }
}
rootProject.name = "PageSnap"
include(":app")
