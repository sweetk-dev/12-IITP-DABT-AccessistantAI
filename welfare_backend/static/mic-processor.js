/**
 * mic-processor.js — AudioWorkletProcessor (Phase 5 정정안: 제3안 채택)
 * ────────────────────────────────────────────────────────────
 * 마이크 입력(브라우저 기본 샘플레이트)을 받아:
 *   1) 16kHz 다운샘플
 *   2) Int16 PCM 변환 — **무음 여부 관계없이 항상 백엔드로 전송**
 *   3) RMS 기반 voice_state 는 **UI 표시용으로만** 산출 (말풍선 애니메이션 등)
 *
 * 🔄 Phase 5 변경 사유:
 *   기존 구조는 프론트 RMS 게이트로 무음 청크를 차단해 백엔드 Gemini Live API 의
 *   자체 VAD 와 충돌 가능성이 있었음. 사용자가 작게 발화하면 첫 음절이 클리핑되거나
 *   Gemini 가 오디오 스트림 단절로 판단해 turn 상태가 꼬일 수 있음.
 *   → 실시간 음성 AI 표준 패턴인 "오디오는 항상 스트림, VAD 는 Gemini 일임" 채택.
 *
 * AudioWorklet 은 별도 오디오 스레드에서 실행되어 메인 UI 스레드를 막지 않음.
 * ScriptProcessorNode(deprecated) 대비 메모리 누수·블로킹 없음.
 *
 * 메인에서 받는 메시지 포맷:
 *   { type: 'pcm', data: Int16Array.buffer, rms: float }            — 모든 청크
 *   { type: 'voice_state', voiced: bool, rms: float }                — 상태 전환 시에만
 *
 * 옵션 (processorOptions 로 전달):
 *   targetSampleRate (기본 16000)
 *   chunkFrames      (기본 2048 — 16kHz 기준 128ms)
 *   rmsThreshold     (기본 0.012 — voice_state UI 표시 임계값)
 *   voicedRunFrames  (기본 2 — 연속 N 청크 voiced 면 voiced 상태 진입)
 */
class MicProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const opts = (options && options.processorOptions) || {};
    this.targetSR   = opts.targetSampleRate ?? 16000;
    this.chunkFrames = opts.chunkFrames      ?? 2048;
    this.rmsThreshold = opts.rmsThreshold    ?? 0.012;
    this.voicedRunFrames = opts.voicedRunFrames ?? 2;

    // 입력 샘플레이트(브라우저 기본, 보통 44100/48000)와 목표(16000)의 비율
    this.inputSR = sampleRate;          // global in AudioWorklet
    this.ratio = this.inputSR / this.targetSR;

    // 다운샘플 누적 버퍼 (Float32 16kHz)
    this.downBuffer = new Float32Array(this.chunkFrames * 2);
    this.downIndex = 0;

    // 다운샘플 보조
    this.inputAccumulator = 0;
    this.inputCount = 0;
    this.sampleEvery = this.ratio;
    this.sampleCursor = 0;

    // 발화 감지 상태
    this.consecutiveVoiced = 0;
    this.isCurrentlyVoiced = false;

    this.port.onmessage = (e) => {
      const m = e.data || {};
      if (m.type === 'config') {
        if (typeof m.rmsThreshold === 'number') this.rmsThreshold = m.rmsThreshold;
        if (typeof m.chunkFrames === 'number') this.chunkFrames = m.chunkFrames;
      }
    };
  }

  /**
   * 매 128 샘플 호출됨 (브라우저 표준). 입력 1채널 가정.
   */
  process(inputs) {
    const input = inputs[0];
    if (!input || input.length === 0) return true;
    const ch = input[0]; // mono
    if (!ch || ch.length === 0) return true;

    // 1) 다운샘플 — 단순 평균법 (anti-aliasing 약식)
    for (let i = 0; i < ch.length; i++) {
      this.inputAccumulator += ch[i];
      this.inputCount++;
      this.sampleCursor++;
      if (this.sampleCursor >= this.sampleEvery) {
        const avg = this.inputAccumulator / this.inputCount;
        this.inputAccumulator = 0;
        this.inputCount = 0;
        this.sampleCursor -= this.sampleEvery;

        if (this.downIndex < this.downBuffer.length) {
          this.downBuffer[this.downIndex++] = avg;
        }

        if (this.downIndex >= this.chunkFrames) {
          this.flushChunk();
        }
      }
    }
    return true;
  }

  flushChunk() {
    const frames = this.downIndex;
    // 2) RMS 계산 — UI 표시용 voice_state 만 산출 (오디오 전송 게이트로는 사용하지 않음)
    let sumSq = 0;
    for (let i = 0; i < frames; i++) sumSq += this.downBuffer[i] * this.downBuffer[i];
    const rms = Math.sqrt(sumSq / frames);
    const voiced = rms >= this.rmsThreshold;

    if (voiced) {
      this.consecutiveVoiced++;
    } else {
      this.consecutiveVoiced = 0;
    }
    const newVoicedState = this.consecutiveVoiced >= this.voicedRunFrames;

    // UI 상태 전환 이벤트 (말풍선·인디케이터용) — 실제 인터럽션 판단은 Gemini AAD 에 일임
    if (newVoicedState !== this.isCurrentlyVoiced) {
      this.isCurrentlyVoiced = newVoicedState;
      this.port.postMessage({ type: 'voice_state', voiced: newVoicedState, rms });
    }

    // 3) Int16 PCM 변환 — **항상 백엔드로 전송**
    //    Gemini Live API 의 automatic_activity_detection 이 VAD/turn 경계를 판정.
    //    프론트에서 무음 청크를 끊으면 첫 음절 클리핑 + Gemini 의 스트림 단절 오인 위험.
    const pcm = new Int16Array(frames);
    for (let i = 0; i < frames; i++) {
      const s = Math.max(-1, Math.min(1, this.downBuffer[i]));
      pcm[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }
    // transferable 로 zero-copy 전송
    this.port.postMessage({ type: 'pcm', data: pcm.buffer, rms }, [pcm.buffer]);

    // 4) 다운 버퍼 리셋 (잔여 샘플 보존 위해 큰 버퍼 활용 — 다음 호출에 이어 채움)
    this.downIndex = 0;
  }
}

registerProcessor('mic-processor', MicProcessor);
