# 이동경로 안내 앱 (Android)

서비스 화면(`/navi`)을 담는 껍데기 앱이다. 화면과 안내 로직은 웹에 있고, 앱은 브라우저로는 안 되는 것만 맡는다.
무엇을 맡는지는 저장소 루트 `README.md` 의 "제품 분리" 절을 본다.

## 구조

| 파일 | 역할 |
|------|------|
| `MainActivity.kt` | WebView, 접속 계정 입력, 권한 전달, 뒤로 가기, 통화 감지, 넘겨받은 목적지 처리 |
| `GuideService.kt` | 안내 중 포그라운드 서비스(위치·마이크) — 화면이 꺼져도 안내 유지 |
| `AppUpdater.kt` | 새 버전 확인 · 내려받기 · 해시 확인 · 설치 |
| `Prefs.kt` | 접속 계정 보관(앱 전용 저장소) |

화면과 주고받는 것

- 화면 → 앱: `AccessNaviApp.setBusy(bool)` (상담·안내 진행 여부), `AccessNaviApp.micGranted()`, `AccessNaviApp.logout()`(로그아웃), `AccessNaviApp.reload()`
- 앱 → 화면: `NAVI.backKey()` (뒤로 가기 키 — true 면 화면이 처리, false 면 앱이 종료를 묻는다), `NAVI.handoff(...)` (새 목적지), `__APP.onAudioFocus(bool)` (통화 시작·끝)
- 앱 안에서 열린 화면은 사용자 에이전트에 `AccessNaviApp/<버전>` 이 붙는다

## 빌드

```
# local.properties
sdk.dir=<Android SDK 경로>
accessnavi.baseUrl=https://<서비스 주소>

./gradlew assembleRelease -Paccessnavi.keystoreProps=<keystore.properties 경로>
```

`local.properties`, `keystore.properties`, 키 파일, 빌드 산출물은 저장소에 두지 않는다(`.gitignore`).
서명 정보가 없으면 release 빌드는 서명되지 않는다.

## 배포본 올리기

서버의 `APP_RELEASE_DIR` 에 설치 파일과 `latest.json` 을 둔다.

```json
{"versionCode": 20000, "versionName": "2.0.0", "file": "accessnavi-2.0.0.apk",
 "sha256": "<설치 파일 SHA-256>", "notes": "바뀐 점"}
```

새 버전을 낼 때는 `app/build.gradle.kts` 의 `versionCode`·`versionName` 을 올린다. 같은 키로 서명해야 덮어 설치된다.
