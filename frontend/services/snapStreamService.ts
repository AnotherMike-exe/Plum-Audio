/**
 * SnapStream Service - Browser Audio Client for Snapcast
 *
 * Connects to Snapcast server via WebSocket at ws://host:1780/stream
 * and plays audio directly in the browser using Web Audio API.
 *
 * Based on Snapweb implementation: https://github.com/badaix/snapweb
 */

// Time value container (seconds + microseconds)
class Tv {
    sec: number = 0;
    usec: number = 0;

    constructor(sec: number, usec: number) {
        this.sec = sec;
        this.usec = usec;
    }

    setMilliseconds(ms: number) {
        this.sec = Math.floor(ms / 1000);
        this.usec = Math.floor(ms * 1000) % 1000000;
    }

    getMilliseconds(): number {
        return this.sec * 1000 + this.usec / 1000;
    }
}

// Base message format for Snapcast binary protocol
class BaseMessage {
    type: number = 0;
    id: number = 0;
    refersTo: number = 0;
    received: Tv = new Tv(0, 0);
    sent: Tv = new Tv(0, 0);
    size: number = 0;

    deserialize(buffer: ArrayBuffer) {
        const view = new DataView(buffer);
        this.type = view.getUint16(0, true);
        this.id = view.getUint16(2, true);
        this.refersTo = view.getUint16(4, true);
        this.received = new Tv(view.getInt32(6, true), view.getInt32(10, true));
        this.sent = new Tv(view.getInt32(14, true), view.getInt32(18, true));
        this.size = view.getUint32(22, true);
    }

    serialize(): ArrayBuffer {
        this.size = 26 + this.getSize();
        const buffer = new ArrayBuffer(this.size);
        const view = new DataView(buffer);
        view.setUint16(0, this.type, true);
        view.setUint16(2, this.id, true);
        view.setUint16(4, this.refersTo, true);
        view.setInt32(6, this.sent.sec, true);
        view.setInt32(10, this.sent.usec, true);
        view.setInt32(14, this.received.sec, true);
        view.setInt32(18, this.received.usec, true);
        view.setUint32(22, this.size, true);
        return buffer;
    }

    getSize(): number {
        return 0;
    }
}

// JSON message wrapper
class JsonMessage extends BaseMessage {
    json: any;

    deserialize(buffer: ArrayBuffer) {
        super.deserialize(buffer);
        const view = new DataView(buffer);
        const size = view.getUint32(26, true);
        const decoder = new TextDecoder();
        this.json = JSON.parse(decoder.decode(buffer.slice(30, 30 + size)));
    }

    serialize(): ArrayBuffer {
        const buffer = super.serialize();
        const view = new DataView(buffer);
        const jsonStr = JSON.stringify(this.json);
        view.setUint32(26, jsonStr.length, true);
        const encoder = new TextEncoder();
        const encoded = encoder.encode(jsonStr);
        for (let i = 0; i < encoded.length; ++i)
            view.setUint8(30 + i, encoded[i]);
        return buffer;
    }

    getSize(): number {
        const encoder = new TextEncoder();
        const encoded = encoder.encode(JSON.stringify(this.json));
        return encoded.length + 4;
    }
}

// Hello message - client identification
class HelloMessage extends JsonMessage {
    mac: string = "";
    hostname: string = "";
    version: string = "0.1.0";
    clientName: string = "Plum Audio Browser Client";
    os: string = "";
    arch: string = "web";
    instance: number = 1;
    uniqueId: string = "";
    snapStreamProtocolVersion: number = 2;

    constructor(buffer?: ArrayBuffer) {
        super();
        if (buffer) {
            this.deserialize(buffer);
        }
        this.type = 5;
    }

    serialize(): ArrayBuffer {
        this.json = {
            "MAC": this.mac,
            "HostName": this.hostname,
            "Version": this.version,
            "ClientName": this.clientName,
            "OS": this.os,
            "Arch": this.arch,
            "Instance": this.instance,
            "ID": this.uniqueId,
            "SnapStreamProtocolVersion": this.snapStreamProtocolVersion
        };
        return super.serialize();
    }
}

// Time synchronization message
class TimeMessage extends BaseMessage {
    latency: Tv = new Tv(0, 0);

    constructor(buffer?: ArrayBuffer) {
        super();
        if (buffer) {
            this.deserialize(buffer);
        }
        this.type = 4;
    }

    deserialize(buffer: ArrayBuffer) {
        super.deserialize(buffer);
        const view = new DataView(buffer);
        this.latency = new Tv(view.getInt32(26, true), view.getInt32(30, true));
    }

    serialize(): ArrayBuffer {
        const buffer = super.serialize();
        const view = new DataView(buffer);
        view.setInt32(26, this.latency.sec, true);
        view.setInt32(30, this.latency.usec, true);
        return buffer;
    }

    getSize(): number {
        return 8;
    }
}

// Server settings message
class ServerSettingsMessage extends JsonMessage {
    bufferMs: number = 0;
    latency: number = 0;
    volumePercent: number = 0;
    muted: boolean = false;

    constructor(buffer?: ArrayBuffer) {
        super();
        if (buffer) {
            this.deserialize(buffer);
        }
        this.type = 3;
    }

    deserialize(buffer: ArrayBuffer) {
        super.deserialize(buffer);
        this.bufferMs = this.json["bufferMs"];
        this.latency = this.json["latency"];
        this.volumePercent = this.json["volume"];
        this.muted = this.json["muted"];
    }
}

// Codec header message
class CodecMessage extends BaseMessage {
    codec: string = "";
    payload: ArrayBuffer;

    constructor(buffer?: ArrayBuffer) {
        super();
        this.payload = new ArrayBuffer(0);
        if (buffer) {
            this.deserialize(buffer);
        }
        this.type = 1;
    }

    deserialize(buffer: ArrayBuffer) {
        super.deserialize(buffer);
        const view = new DataView(buffer);
        const codecSize = view.getInt32(26, true);
        const decoder = new TextDecoder("utf-8");
        this.codec = decoder.decode(buffer.slice(30, 30 + codecSize));
        const payloadSize = view.getInt32(30 + codecSize, true);
        this.payload = buffer.slice(34 + codecSize, 34 + codecSize + payloadSize);
    }
}

// Sample format info
class SampleFormat {
    rate: number = 48000;
    channels: number = 2;
    bits: number = 16;

    msRate(): number {
        return this.rate / 1000;
    }

    toString(): string {
        return `${this.rate}:${this.bits}:${this.channels}`;
    }

    sampleSize(): number {
        if (this.bits === 24) {
            return 4;
        }
        return this.bits / 8;
    }

    frameSize(): number {
        return this.channels * this.sampleSize();
    }
}

// PCM audio chunk
class PcmChunkMessage extends BaseMessage {
    timestamp: Tv = new Tv(0, 0);
    payload: ArrayBuffer = new ArrayBuffer(0);
    idx: number = 0;
    sampleFormat: SampleFormat;

    constructor(buffer: ArrayBuffer, sampleFormat: SampleFormat) {
        super();
        this.sampleFormat = sampleFormat;
        this.deserialize(buffer);
        this.type = 2;
    }

    deserialize(buffer: ArrayBuffer) {
        super.deserialize(buffer);
        const view = new DataView(buffer);
        this.timestamp = new Tv(view.getInt32(26, true), view.getInt32(30, true));
        this.payload = buffer.slice(38);
    }

    readFrames(frames: number): ArrayBuffer {
        let frameCnt = frames;
        const frameSize = this.sampleFormat.frameSize();
        if (this.idx + frames > this.payloadSize() / frameSize)
            frameCnt = (this.payloadSize() / frameSize) - this.idx;
        const begin = this.idx * frameSize;
        this.idx += frameCnt;
        const end = begin + frameCnt * frameSize;
        return this.payload.slice(begin, end);
    }

    getFrameCount(): number {
        return (this.payloadSize() / this.sampleFormat.frameSize());
    }

    isEndOfChunk(): boolean {
        return this.idx >= this.getFrameCount();
    }

    startMs(): number {
        return this.timestamp.getMilliseconds() + 1000 * (this.idx / this.sampleFormat.rate);
    }

    duration(): number {
        return 1000 * ((this.getFrameCount() - this.idx) / this.sampleFormat.rate);
    }

    payloadSize(): number {
        return this.payload.byteLength;
    }

    clearPayload(): void {
        this.payload = new ArrayBuffer(0);
    }

    addPayload(buffer: ArrayBuffer) {
        const payload = new ArrayBuffer(this.payload.byteLength + buffer.byteLength);
        const view = new DataView(payload);
        const viewOld = new DataView(this.payload);
        const viewNew = new DataView(buffer);
        for (let i = 0; i < viewOld.byteLength; ++i) {
            view.setInt8(i, viewOld.getInt8(i));
        }
        for (let i = 0; i < viewNew.byteLength; ++i) {
            view.setInt8(i + viewOld.byteLength, viewNew.getInt8(i));
        }
        this.payload = payload;
    }
}

// Time synchronization provider
class TimeProvider {
    private ctx?: AudioContext;
    private diffBuffer: Array<number> = [];
    private diff: number = 0;

    setAudioContext(ctx: AudioContext) {
        this.ctx = ctx;
        this.reset();
    }

    reset() {
        this.diffBuffer.length = 0;
        this.diff = 0;
    }

    setDiff(c2s: number, s2c: number) {
        if (this.now() === 0) {
            this.reset();
        } else {
            if (this.diffBuffer.push((c2s - s2c) / 2) > 100)
                this.diffBuffer.shift();
            const sorted = [...this.diffBuffer];
            sorted.sort();
            this.diff = sorted[Math.floor(sorted.length / 2)];
        }
    }

    now(): number {
        if (!this.ctx) {
            return window.performance.now();
        } else {
            return this.ctx.currentTime * 1000;
        }
    }

    nowSec(): number {
        return this.now() / 1000;
    }

    serverNow(): number {
        return this.serverTime(this.now());
    }

    serverTime(localTimeMs: number): number {
        return localTimeMs + this.diff;
    }
}

// Audio stream buffer management
class AudioStream {
    private chunks: Array<PcmChunkMessage> = [];
    private chunk?: PcmChunkMessage;
    private volume: number = 1;
    private muted: boolean = false;
    private lastLog: number = 0;

    constructor(
        private timeProvider: TimeProvider,
        private sampleFormat: SampleFormat,
        private bufferMs: number
    ) {}

    setVolume(percent: number, muted: boolean) {
        this.volume = percent / 100;
        this.muted = muted;
        console.log(`setVolume: ${percent} => ${this.volume}, muted: ${muted}`);
    }

    addChunk(chunk: PcmChunkMessage) {
        this.chunks.push(chunk);

        // Drop old chunks
        while (this.chunks.length > 0) {
            const age = this.timeProvider.serverNow() - this.chunks[0].timestamp.getMilliseconds();
            if (age > 5000 + this.bufferMs) {
                this.chunks.shift();
                console.log(`Dropping old chunk: ${age.toFixed(2)}, left: ${this.chunks.length}`);
            } else {
                break;
            }
        }
    }

    getNextBuffer(buffer: AudioBuffer, playTimeMs: number) {
        if (!this.chunk) {
            this.chunk = this.chunks.shift();
        }

        const frames = buffer.length;
        const left = new Float32Array(frames);
        const right = new Float32Array(frames);
        let read = 0;
        let pos = 0;
        const serverPlayTimeMs = this.timeProvider.serverTime(playTimeMs);

        if (this.chunk) {
            let age = serverPlayTimeMs - this.chunk.startMs();
            const reqChunkDuration = frames / this.sampleFormat.msRate();

            if (age < -reqChunkDuration) {
                console.log("Chunk too young, returning silence");
            } else {
                // Hard sync if more than 5ms off
                if (Math.abs(age) > 5) {
                    while (this.chunk && age > this.chunk.duration()) {
                        console.log(`Chunk too old, dropping (age: ${age.toFixed(2)} > ${this.chunk.duration().toFixed(2)})`);
                        this.chunk = this.chunks.shift();
                        if (!this.chunk) break;
                        age = serverPlayTimeMs - this.chunk.startMs();
                    }
                    if (this.chunk) {
                        if (age > 0) {
                            console.log(`Fast forwarding ${age.toFixed(2)}ms`);
                            this.chunk.readFrames(Math.floor(age * this.chunk.sampleFormat.msRate()));
                        } else if (age < 0) {
                            console.log(`Playing silence ${(-age).toFixed(2)}ms`);
                            const silentFrames = Math.floor(-age * this.chunk.sampleFormat.msRate());
                            left.fill(0, 0, silentFrames);
                            right.fill(0, 0, silentFrames);
                            read = silentFrames;
                            pos = silentFrames;
                        }
                        age = 0;
                    }
                }

                // Read audio data
                const readFrames = frames - read;
                while ((read < readFrames) && this.chunk) {
                    const pcmChunk = this.chunk;
                    const pcmBuffer = pcmChunk.readFrames(readFrames - read);
                    const normalize: number = 2 ** pcmChunk.sampleFormat.bits;
                    let payload: Int16Array | Int32Array;

                    if (pcmChunk.sampleFormat.bits >= 24)
                        payload = new Int16Array(pcmBuffer);
                    else
                        payload = new Int16Array(pcmBuffer);

                    for (let i = 0; i < payload.length; i += 2) {
                        read++;
                        left[pos] = (payload[i] / normalize);
                        right[pos] = (payload[i + 1] / normalize);
                        pos++;
                    }

                    if (pcmChunk.isEndOfChunk()) {
                        this.chunk = this.chunks.shift();
                    }
                }
            }
        }

        if (read < frames) {
            console.log(`Failed to get chunk, read: ${read}/${frames}, chunks left: ${this.chunks.length}`);
            left.fill(0, pos);
            right.fill(0, pos);
        }

        // Apply volume
        const vol = this.muted ? 0 : this.volume;
        for (let i = 0; i < frames; i++) {
            left[i] *= vol;
            right[i] *= vol;
        }

        buffer.getChannelData(0).set(left);
        buffer.getChannelData(1).set(right);
    }
}

// Generate UUID v4
function uuidv4(): string {
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function (c) {
        const r = Math.random() * 16 | 0, v = c === 'x' ? r : ((r & 0x3) | 0x8);
        return v.toString(16);
    });
}

// Get/set persistent value in localStorage
function getPersistentValue(key: string, defaultValue: string): string {
    const stored = localStorage.getItem(`snapstream_${key}`);
    if (stored) return stored;
    localStorage.setItem(`snapstream_${key}`, defaultValue);
    return defaultValue;
}

// Main SnapStream class
export class SnapStream {
    private baseUrl: string;
    private streamsocket?: WebSocket;
    private ctx?: AudioContext;
    private gainNode?: GainNode;
    private analyserNode?: AnalyserNode; // For visualizer
    private timeProvider: TimeProvider;
    private stream?: AudioStream;
    private sampleFormat?: SampleFormat;
    private serverSettings?: ServerSettingsMessage;
    private msgId: number = 0;
    private syncHandle: number = -1;
    private reconnectHandle: number = -1;
    private playTime: number = 0;
    private audioBuffers: Array<any> = [];
    private freeBuffers: Array<AudioBuffer> = [];
    private bufferMs: number = 1000;
    private bufferFrameCount: number = 3844;
    private bufferDurationMs: number = 80;
    private audioBufferCount: number = 3;
    private latency: number = 0;
    private bufferNum: number = 0;
    private isPlaying: boolean = false;
    private connectionPromise?: Promise<void>;
    private connectionResolve?: () => void;
    private connectionReject?: (error: Error) => void;

    constructor(host: string, port: number = 1780) {
        this.baseUrl = `ws://${host}:${port}`;
        this.timeProvider = new TimeProvider();
    }

    /**
     * Start the audio stream with connection retry
     */
    async start(maxRetries: number = 3): Promise<void> {
        if (this.isPlaying) {
            console.warn("Already playing");
            return;
        }

        // Check Web Audio API support (don't create context yet - wait for codec header to know sample rate)
        if (!window.AudioContext) {
            throw new Error("Web Audio API is not supported by your browser");
        }

        // Try to connect with retries
        let lastError: Error | undefined;
        for (let attempt = 1; attempt <= maxRetries; attempt++) {
            try {
                await this.connectWithTimeout(5000);
                this.isPlaying = true;
                return;
            } catch (error) {
                lastError = error as Error;
                console.warn(`SnapStream connection attempt ${attempt}/${maxRetries} failed:`, error);
                if (attempt < maxRetries) {
                    // Wait before retry (increasing delay)
                    await new Promise(resolve => setTimeout(resolve, 500 * attempt));
                }
            }
        }

        throw lastError || new Error("Failed to connect after retries");
    }

    /**
     * Connect with timeout - returns promise that resolves when connected
     */
    private connectWithTimeout(timeoutMs: number): Promise<void> {
        return new Promise((resolve, reject) => {
            this.connectionResolve = resolve;
            this.connectionReject = reject;

            // Set timeout for connection
            const timeout = setTimeout(() => {
                if (this.streamsocket) {
                    this.streamsocket.close();
                }
                reject(new Error(`Connection timeout after ${timeoutMs}ms`));
            }, timeoutMs);

            // Create connection promise that clears timeout on success
            this.connectionPromise = new Promise<void>((res, rej) => {
                this.connectionResolve = () => {
                    clearTimeout(timeout);
                    res();
                    resolve();
                };
                this.connectionReject = (err: Error) => {
                    clearTimeout(timeout);
                    rej(err);
                    reject(err);
                };
            });

            this.connect();
        });
    }

    /**
     * Stop the audio stream and disconnect
     */
    stop(): void {
        if (!this.isPlaying) return;

        window.clearInterval(this.syncHandle);
        window.clearTimeout(this.reconnectHandle); // Cancel any pending reconnection
        this.stopAudio();
        if (this.streamsocket && (this.streamsocket.readyState === WebSocket.OPEN || this.streamsocket.readyState === WebSocket.CONNECTING)) {
            this.streamsocket.onclose = () => {};
            this.streamsocket.close();
        }
        this.isPlaying = false;
    }

    /**
     * Set volume (0-100)
     */
    setVolume(percent: number, muted: boolean = false): void {
        if (this.gainNode) {
            this.gainNode.gain.value = muted ? 0 : percent / 100;
        }
        if (this.stream) {
            this.stream.setVolume(percent, muted);
        }
    }

    /**
     * Get current frequency data for visualizer
     */
    getFrequencyData(): Uint8Array | null {
        if (!this.analyserNode) return null;

        const bufferLength = this.analyserNode.frequencyBinCount;
        const dataArray = new Uint8Array(bufferLength);
        this.analyserNode.getByteFrequencyData(dataArray);
        return dataArray;
    }

    /**
     * Update analyser settings (called when visualizer settings change)
     */
    updateAnalyserSettings(fftSize: number, smoothing: number): void {
        if (this.analyserNode) {
            this.analyserNode.fftSize = fftSize;
            this.analyserNode.smoothingTimeConstant = smoothing / 100;
        }
    }

    /**
     * Resume audio context (needed after user interaction)
     */
    resume(): void {
        this.ctx?.resume();
    }

    /**
     * Get unique client ID
     */
    static getClientId(): string {
        return getPersistentValue("uniqueId", uuidv4());
    }

    /**
     * Check if currently playing
     */
    getIsPlaying(): boolean {
        return this.isPlaying;
    }

    private setupAudioContext(): boolean {
        if (!window.AudioContext) {
            return false;
        }

        // If AudioContext already exists with correct sample rate, reuse it
        if (this.ctx && this.ctx.sampleRate === this.sampleFormat?.rate) {
            console.log(`Reusing existing AudioContext with sample rate ${this.ctx.sampleRate}`);
            return true;
        }

        // Close existing context if sample rate changed
        if (this.ctx) {
            console.log(`Closing existing AudioContext (rate ${this.ctx.sampleRate}) for new rate ${this.sampleFormat?.rate}`);
            this.ctx.close();
        }

        const options: AudioContextOptions = {
            latencyHint: "interactive",
            sampleRate: this.sampleFormat?.rate
        };

        this.ctx = new AudioContext(options);
        console.log(`Created AudioContext with sample rate ${this.ctx.sampleRate}`);

        // Create AnalyserNode for visualizer (BEFORE GainNode for volume-independent analysis)
        this.analyserNode = this.ctx.createAnalyser();
        this.analyserNode.fftSize = 2048;
        this.analyserNode.smoothingTimeConstant = 0.6;

        this.gainNode = this.ctx.createGain();

        // Connect: Analyser → Gain → Destination
        this.analyserNode.connect(this.gainNode);
        this.gainNode.connect(this.ctx.destination);
        return true;
    }

    private connect() {
        this.streamsocket = new WebSocket(this.baseUrl + '/stream');
        this.streamsocket.binaryType = "arraybuffer";
        this.streamsocket.onmessage = (ev) => this.onMessage(ev);

        this.streamsocket.onopen = () => {
            console.log("SnapStream connected to " + this.baseUrl);
            const hello = new HelloMessage();
            hello.mac = "00:00:00:00:00:00";
            hello.arch = "web";
            hello.os = navigator?.platform || "unknown";
            hello.hostname = "This Browser";
            hello.uniqueId = SnapStream.getClientId();
            this.sendMessage(hello);
            this.syncTime();
            this.syncHandle = window.setInterval(() => this.syncTime(), 1000);

            // Resolve connection promise if waiting
            if (this.connectionResolve) {
                this.connectionResolve();
                this.connectionResolve = undefined;
                this.connectionReject = undefined;
            }
        };

        this.streamsocket.onerror = (ev) => {
            console.error('SnapStream error:', ev);
            // Reject connection promise if waiting (connection failed before open)
            if (this.connectionReject) {
                this.connectionReject(new Error('WebSocket connection failed'));
                this.connectionResolve = undefined;
                this.connectionReject = undefined;
            }
        };

        this.streamsocket.onclose = () => {
            window.clearInterval(this.syncHandle);
            console.info('SnapStream connection lost');

            // Reject connection promise if waiting (connection closed before fully established)
            if (this.connectionReject) {
                this.connectionReject(new Error('WebSocket connection closed'));
                this.connectionResolve = undefined;
                this.connectionReject = undefined;
            }

            if (this.isPlaying) {
                console.info('Reconnecting in 1s');
                this.reconnectHandle = window.setTimeout(() => this.connect(), 1000);
            }
        };
    }

    private onMessage(msg: MessageEvent) {
        const view = new DataView(msg.data);
        const type = view.getUint16(0, true);

        if (type === 1) {
            // Codec header
            const codec = new CodecMessage(msg.data);
            console.log("Codec: " + codec.codec);

            if (codec.codec === "pcm") {
                this.sampleFormat = this.decodePcmHeader(codec.payload);
                console.log("Sample format: " + this.sampleFormat.toString());

                if ((this.sampleFormat.channels !== 2) || (this.sampleFormat.bits < 16)) {
                    alert("Stream must be stereo with 16, 24 or 32 bit depth, actual format: " + this.sampleFormat.toString());
                } else {
                    if (this.bufferDurationMs !== 0) {
                        this.bufferFrameCount = Math.floor(this.bufferDurationMs * this.sampleFormat.msRate());
                    }

                    // Create AudioContext NOW with the correct sample rate from the stream
                    // This prevents sample rate mismatch that causes "wobbly" audio on first listen
                    if (!this.setupAudioContext()) {
                        console.error("Failed to setup AudioContext");
                        return;
                    }

                    this.ctx!.resume();
                    this.timeProvider.setAudioContext(this.ctx!);
                    this.gainNode!.gain.value = this.serverSettings?.muted ? 0 : (this.serverSettings?.volumePercent || 100) / 100;
                    this.stream = new AudioStream(this.timeProvider, this.sampleFormat, this.bufferMs);
                    this.latency = (this.ctx!.baseLatency || 0) + ((this.ctx! as any).outputLatency || 0);
                    console.log("Latency: " + this.latency);
                    this.play();
                }
            } else {
                alert("Codec not supported: " + codec.codec + ". Only PCM is supported.");
            }
        } else if (type === 2) {
            // Audio chunk
            const pcmChunk = new PcmChunkMessage(msg.data, this.sampleFormat!);
            this.stream?.addChunk(pcmChunk);
        } else if (type === 3) {
            // Server settings
            this.serverSettings = new ServerSettingsMessage(msg.data);
            if (this.gainNode) {
                this.gainNode.gain.value = this.serverSettings.muted ? 0 : this.serverSettings.volumePercent / 100;
            }
            this.bufferMs = this.serverSettings.bufferMs - this.serverSettings.latency;
            console.log(`Server settings: bufferMs=${this.serverSettings.bufferMs}, latency=${this.serverSettings.latency}, volume=${this.serverSettings.volumePercent}, muted=${this.serverSettings.muted}`);
        } else if (type === 4) {
            // Time sync
            const time = new TimeMessage(msg.data);
            this.timeProvider.setDiff(time.latency.getMilliseconds(), this.timeProvider.now() - time.sent.getMilliseconds());
        }
    }

    private decodePcmHeader(buffer: ArrayBuffer): SampleFormat {
        const sampleFormat = new SampleFormat();
        const view = new DataView(buffer);
        sampleFormat.channels = view.getUint16(22, true);
        sampleFormat.rate = view.getUint32(24, true);
        sampleFormat.bits = view.getUint16(34, true);
        return sampleFormat;
    }

    private sendMessage(msg: BaseMessage) {
        msg.sent = new Tv(0, 0);
        msg.sent.setMilliseconds(this.timeProvider.now());
        msg.id = ++this.msgId;
        if (this.streamsocket && this.streamsocket.readyState === WebSocket.OPEN) {
            this.streamsocket.send(msg.serialize());
        }
    }

    private syncTime() {
        const t = new TimeMessage();
        t.latency.setMilliseconds(this.timeProvider.now());
        this.sendMessage(t);
    }

    private stopAudio() {
        this.ctx?.suspend();
        while (this.audioBuffers.length > 0) {
            const buffer = this.audioBuffers.pop();
            if (buffer) {
                buffer.onended = () => {};
                buffer.source.stop();
            }
        }
        this.freeBuffers.length = 0;
    }

    private play() {
        this.playTime = this.timeProvider.nowSec() + 0.1;
        for (let i = 1; i <= this.audioBufferCount; ++i) {
            this.playNext();
        }
    }

    private playNext() {
        const buffer = this.freeBuffers.pop() || this.ctx!.createBuffer(
            this.sampleFormat!.channels,
            this.bufferFrameCount,
            this.sampleFormat!.rate
        );
        const playTimeMs = (this.playTime + this.latency) * 1000 - this.bufferMs;
        this.stream!.getNextBuffer(buffer, playTimeMs);

        const source = this.ctx!.createBufferSource();
        source.buffer = buffer;
        // Connect to analyserNode for visualizer (falls back to gainNode if no analyser)
        source.connect(this.analyserNode || this.gainNode!);

        const playBuffer = {
            buffer,
            playTime: this.playTime,
            source,
            num: ++this.bufferNum,
            onended: null as any
        };

        this.audioBuffers.push(playBuffer);

        playBuffer.onended = (pb: any) => {
            this.freeBuffers.push(this.audioBuffers.splice(this.audioBuffers.indexOf(pb), 1)[0].buffer);
            this.playNext();
        };

        source.onended = () => {
            if (playBuffer.onended) playBuffer.onended(playBuffer);
        };

        source.start(this.playTime);
        this.playTime += this.bufferFrameCount / this.sampleFormat!.rate;
    }
}
