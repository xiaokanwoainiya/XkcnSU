@file:Suppress("UnstableApiUsage")

enableFeaturePreview("TYPESAFE_PROJECT_ACCESSORS")

pluginManagement {
    repositories {
        gradlePluginPortal()
        google()
        mavenCentral()
    }
}

dependencyResolutionManagement {
    repositories {
        providers.environmentVariable("XKCNSU_KOTLIN_MAVEN").orNull?.let { compilerRepository ->
            maven {
                url = uri(compilerRepository)
                content { includeVersion("org.jetbrains.kotlin", "kotlin-compiler-embeddable", "2.4.20") }
            }
        }
        google()
        mavenCentral()
        maven("https://jitpack.io")
    }
}

rootProject.name = "XKCNSU"
include(":app")
