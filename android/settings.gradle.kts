pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}
dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
        // 카카오맵 Android SDK(v2) 배포 저장소 — 길안내 지도에만 쓴다 (app/build.gradle.kts 의존성 주석 참조)
        maven("https://devrepo.kakao.com/nexus/repository/kakaomap-releases/")
    }
}
rootProject.name = "AccessNavi"
include(":app")
