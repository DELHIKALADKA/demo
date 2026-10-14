plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
}

android {
    namespace = "app.pagesnap"
    compileSdk = 35

    defaultConfig {
        applicationId = "app.pagesnap"
        minSdk = 24
        targetSdk = 35
        versionCode = 1
        versionName = "0.1.0"
    }

    buildTypes {
        release {
            isMinifyEnabled = true
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro",
            )
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
    buildFeatures { compose = true }
    packaging {
        // yt-dlp ships python + binary libs; keep them uncompressed so they can
        // be loaded straight from the APK.
        jniLibs { useLegacyPackaging = true }
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.activity:activity-compose:1.9.3")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.7")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.9.0")

    val compose = platform("androidx.compose:compose-bom:2024.10.01")
    implementation(compose)
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-tooling-preview")
    debugImplementation("androidx.compose.ui:ui-tooling")

    // The link downloader: the same yt-dlp engine used on the desktop, bundled so
    // the phone does the downloading itself - no server anywhere.
    //
    // This is the actively maintained fork (the one modern downloader apps use);
    // the original com.github.yausername coordinates stop at 0.17.0.
    //
    // LICENSE NOTE: this library is GPL-3.0. Bundling it means the app as a whole
    // has to be GPL-3.0 and its source has to stay available. yt-dlp itself is
    // public domain, but the Android wrapper is not. If that is ever a problem,
    // everything except this one dependency is unaffected - the rest of PageSnap
    // only uses the Android SDK.
    implementation("io.github.junkfood02.youtubedl-android:library:0.18.1")
    implementation("io.github.junkfood02.youtubedl-android:ffmpeg:0.18.1")

    testImplementation("junit:junit:4.13.2")
}
