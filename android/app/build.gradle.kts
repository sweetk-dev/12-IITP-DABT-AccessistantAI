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

android {
    namespace = "kr.co.sweetk.accessnavi"
    compileSdk = 35

    defaultConfig {
        applicationId = "kr.co.sweetk.accessnavi"
        minSdk = 26
        targetSdk = 35
        versionCode = 20002
        versionName = "2.0.2"
        buildConfigField("String", "BASE_URL", "\"$baseUrl\"")
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
dependencies {}
