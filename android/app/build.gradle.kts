import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// 접속 주소와 서명 정보는 저장소에 두지 않는다.
//   local.properties      accessnavi.baseUrl=https://<서비스 주소>
//   keystore.properties   storeFile / storePassword / keyAlias / keyPassword
// 둘 다 .gitignore 대상이다. 다른 위치의 서명 정보 파일은 -Paccessnavi.keystoreProps=<경로> 로 넘긴다.
val localProps = Properties().apply {
    val f = rootProject.file("local.properties")
    if (f.exists()) f.inputStream().use { load(it) }
}
val baseUrl: String = ((project.findProperty("accessnavi.baseUrl") as String?)
    ?: localProps.getProperty("accessnavi.baseUrl")
    ?: "https://example.invalid").trimEnd('/')

val keystorePropsFile = (project.findProperty("accessnavi.keystoreProps") as String?)
    ?.let { file(it) } ?: rootProject.file("keystore.properties")
val keystoreProps = Properties().apply {
    if (keystorePropsFile.exists()) keystorePropsFile.inputStream().use { load(it) }
}
val hasReleaseKey = keystoreProps.getProperty("storeFile") != null

// 카카오맵 네이티브 앱 키 — 저장소에 두지 않는다. 다음 순서로 찾는다.
//   1) -Paccessnavi.kakaoNativeKey=<키>  또는 local.properties 의 accessnavi.kakaoNativeKey
//   2) local.properties 의 accessnavi.kakaoKeyFile=<키가 적힌 파일 경로>
//      파일 안에 설명 문구가 함께 있어도 된다 — 32자리 16진수 하나만 뽑아 쓴다.
// 키가 없으면 빈 값으로 빌드되고, 앱은 길안내 지도를 웹 지도로 그린다(지도 엔진 미초기화).
val kakaoNativeKey: String = run {
    val direct = (project.findProperty("accessnavi.kakaoNativeKey") as String?)
        ?: localProps.getProperty("accessnavi.kakaoNativeKey")
    if (!direct.isNullOrBlank()) return@run direct.trim()
    val path = localProps.getProperty("accessnavi.kakaoKeyFile") ?: return@run ""
    val f = file(path)
    if (!f.exists()) return@run ""
    Regex("[0-9a-f]{32}").find(f.readText())?.value ?: ""
}

android {
    namespace = "kr.co.sweetk.accessnavi"
    compileSdk = 35

    defaultConfig {
        applicationId = "kr.co.sweetk.accessnavi"
        minSdk = 26
        targetSdk = 35
        versionCode = 20102
        versionName = "2.1.2"
        buildConfigField("String", "BASE_URL", "\"$baseUrl\"")
        buildConfigField("String", "KAKAO_NATIVE_KEY", "\"$kakaoNativeKey\"")
        // 지도 엔진(네이티브 라이브러리)은 실제 단말용 64비트 ARM 하나만 넣는다 — 설치 파일 크기를 줄인다(32비트 전용 단말은 웹 지도로 그린다).
        // 카카오맵 SDK 는 x86_64 엔진을 배포하지 않는다(2.15.2 확인) — PC 에뮬레이터에서는 웹 지도로 그려진다.
        ndk { abiFilters += listOf("arm64-v8a") }
    }

    signingConfigs {
        if (hasReleaseKey) {
            create("release") {
                storeFile = file(keystoreProps.getProperty("storeFile"))
                storePassword = keystoreProps.getProperty("storePassword")
                keyAlias = keystoreProps.getProperty("keyAlias")
                keyPassword = keystoreProps.getProperty("keyPassword")
            }
        }
    }

    buildTypes {
        debug {
            // 실기기에 배포본(릴리즈 키 서명)이 깔려 있으면 디버그 키 빌드는 덮어 설치가 안 된다(지우면 저장된 접속 계정도 지워짐).
            // -Paccessnavi.debugWithReleaseKey 를 주면 시험 빌드도 릴리즈 키로 서명한다 — 개발용, 배포본 절차와 무관.
            if (hasReleaseKey && project.hasProperty("accessnavi.debugWithReleaseKey")) {
                signingConfig = signingConfigs.getByName("release")
            }
        }
        release {
            isMinifyEnabled = false
            if (hasReleaseKey) signingConfig = signingConfigs.getByName("release")
        }
    }

    buildFeatures { buildConfig = true }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
}

// 외부 라이브러리를 쓰지 않는다 — 플랫폼 API 와 Kotlin 표준 라이브러리만.
// 예외 하나: 예시 서비스(이동경로 안내)의 길안내 지도에 한해 카카오맵 Android SDK(v2) 를 쓴다.
//   웹 지도는 진행 방향으로 지도를 돌릴 수 없어서다. 이 밖의 용도로 넓히지 않는다.
dependencies {
    implementation("com.kakao.maps.open:android:2.15.2")
}
