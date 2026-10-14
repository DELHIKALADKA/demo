plugins {
    id("com.android.application") version "8.6.1" apply false
    id("org.jetbrains.kotlin.android") version "2.0.21" apply false
    id("org.jetbrains.kotlin.plugin.compose") version "2.0.21" apply false
}

// NOTE for the person building this: the engine itself (app/src/main/java/app/pagesnap/engine)
// has no dependencies beyond the Android SDK, so it can also be compiled and unit-tested
// on its own - that is how the Python engine it was ported from is tested in ./engine.
