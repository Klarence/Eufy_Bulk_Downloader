import {
  EufySecurity,
  EufySecurityConfig,
  Device,
  Station,
  LoginOptions,
  DatabaseReturnCode,
  DatabaseQueryLocal,
  CommandName,
  ErrorCode,
} from "eufy-security-client";
import { Readable } from "stream";
import { spawn, ChildProcess } from "child_process";
import * as fs from "fs";
import * as path from "path";
import pino from "pino";

const logger = pino({ name: "eufy-service" });

export interface SimpleDevice {
  serialNumber: string;
  name: string;
  model: string;
  type: number;
  stationSerialNumber: string;
  isCamera: boolean;
}

export interface SimpleStation {
  serialNumber: string;
  name: string;
  model: string;
}

export interface EventRecord {
  id: string;
  deviceSerialNumber: string;
  deviceName: string;
  stationSerialNumber: string;
  storagePath: string;
  hevcStoragePath: string;
  cipherId: number;
  startTime: number;
  endTime: number;
  thumbPath: string;
  hasHuman: boolean;
  videoType: number;
}

export type ConnectionStatus =
  | "disconnected"
  | "connecting"
  | "connected"
  | "tfa_required"
  | "captcha_required"
  | "error";

export interface CaptchaInfo {
  id: string;
  imageBase64: string;
}

const LOCAL_QUERY_TIMEOUT_MS = 60_000;

interface StationCommandResult {
  command_type: number;
  return_code: number;
  customData?: { command?: { name?: string } };
}

/**
 * Turn the eufy P2P layer's failure codes into something actionable.
 *
 * The library reports send/result failures on the generic "station command
 * result" channel, which previously surfaced to users as a bare
 * "Local database query timed out". These two codes distinguish the two very
 * different situations that look identical from the outside.
 */
function describeLocalQueryFailure(returnCode: number): string {
  switch (returnCode) {
    case ErrorCode.ERROR_COMMAND_TIMEOUT:
      return (
        "Local database query failed: the HomeBase acknowledged the command but " +
        "never returned results. Its local database is likely still initialising, " +
        "or the selected date range is too wide for it to scan."
      );
    case ErrorCode.ERROR_CONNECT_TIMEOUT:
      return (
        "Local database query failed: the command was never sent because another " +
        "P2P command was still in flight, so the query aged out of the send queue."
      );
    default:
      return `Local database query failed (code: ${returnCode}, ${
        ErrorCode[returnCode] ?? "unknown"
      })`;
  }
}

export class EufyService {
  private client: EufySecurity | null = null;
  private _status: ConnectionStatus = "disconnected";
  private _captchaInfo: CaptchaInfo | null = null;
  private _errorMessage: string | null = null;

  private activeDownloads = new Map<
    string,
    {
      resolve: () => void;
      reject: (err: Error) => void;
      ffmpeg: ChildProcess | null;
      videoTempPath: string;
      audioTempPath: string;
      outputPath: string;
    }
  >();

  get status(): ConnectionStatus {
    return this._status;
  }

  get captchaInfo(): CaptchaInfo | null {
    return this._captchaInfo;
  }

  get errorMessage(): string | null {
    return this._errorMessage;
  }

  async initialize(): Promise<void> {
    const email = process.env.EUFY_EMAIL;
    const password = process.env.EUFY_PASSWORD;

    if (!email || !password) {
      throw new Error(
        "EUFY_EMAIL and EUFY_PASSWORD must be set in environment variables"
      );
    }

    const persistentDir = path.resolve(__dirname, "..", "persistent");
    if (!fs.existsSync(persistentDir)) {
      fs.mkdirSync(persistentDir, { recursive: true });
    }

    const config: EufySecurityConfig = {
      username: email,
      password: password,
      country: process.env.EUFY_COUNTRY || "US",
      language: "en",
      persistentDir,
      p2pConnectionSetup: parseInt(
        process.env.P2P_CONNECTION_SETUP || "0",
        10
      ),
      pollingIntervalMinutes: 10,
      eventDurationSeconds: 10,
    };

    const sessionPath = path.join(persistentDir, "session.json");
    if (fs.existsSync(sessionPath)) {
      try {
        config.persistentData = fs.readFileSync(sessionPath, "utf-8");
        logger.info("Restored persistent session data");
      } catch {
        logger.warn("Failed to read persistent session data, starting fresh");
      }
    }

    this._status = "connecting";
    this.client = await EufySecurity.initialize(config);
    this.setupEventListeners();

    try {
      await this.client.connect();
      if (this._status === "connecting") {
        this._status = "connected";
        this.savePersistentData();
        logger.info("Connected to Eufy Security");
      }
    } catch (err) {
      // Event listeners may have changed _status to tfa_required/captcha_required
      // during connect() — only treat as error if they didn't.
      const s = this._status as string;
      if (s !== "tfa_required" && s !== "captcha_required") {
        this._status = "error";
        this._errorMessage =
          err instanceof Error ? err.message : "Unknown connection error";
        logger.error({ err }, "Failed to connect to Eufy Security");
      }
    }
  }

  async submitTfaCode(code: string): Promise<void> {
    if (!this.client) throw new Error("Client not initialized");

    this._status = "connecting";
    try {
      await this.client.connect({ verifyCode: code } as LoginOptions);
      this._status = "connected";
      this.savePersistentData();
      logger.info("Connected after 2FA verification");
    } catch (err) {
      this._status = "error";
      this._errorMessage =
        err instanceof Error ? err.message : "2FA verification failed";
      throw err;
    }
  }

  async submitCaptcha(captchaId: string, captchaCode: string): Promise<void> {
    if (!this.client) throw new Error("Client not initialized");

    this._status = "connecting";
    try {
      await this.client.connect({
        captcha: { captchaId, captchaCode },
      } as LoginOptions);
      this._status = "connected";
      this.savePersistentData();
      logger.info("Connected after captcha verification");
    } catch (err) {
      this._status = "error";
      this._errorMessage =
        err instanceof Error ? err.message : "Captcha verification failed";
      throw err;
    }
  }

  async getDevices(): Promise<SimpleDevice[]> {
    this.ensureConnected();
    const devices: Device[] = await this.client!.getDevices();

    return devices
      .filter((d) => d.isCamera())
      .map((d) => ({
        serialNumber: d.getSerial(),
        name: d.getName(),
        model: d.getModel(),
        type: d.getDeviceType(),
        stationSerialNumber: d.getStationSerial(),
        isCamera: d.isCamera(),
      }));
  }

  async getStations(): Promise<SimpleStation[]> {
    this.ensureConnected();
    const stations: Station[] = await this.client!.getStations();

    return stations.map((s) => ({
      serialNumber: s.getSerial(),
      name: s.getName(),
      model: s.getModel(),
    }));
  }

  /**
   * Fetch video events for a device within a time range.
   *
   * Strategy: try the cloud API first (fast), then fall back to querying
   * the HomeBase's local database via P2P, which is where most users'
   * events actually live.
   */
  async getEvents(
    deviceSerialNumber: string,
    from: Date,
    to: Date
  ): Promise<EventRecord[]> {
    this.ensureConnected();

    logger.info(
      { deviceSN: deviceSerialNumber, from: from.toISOString(), to: to.toISOString() },
      "Fetching events"
    );

    // --- Try cloud API first (quick HTTP calls) ---
    const cloudEvents = await this.getCloudEvents(deviceSerialNumber, from, to);
    if (cloudEvents.length > 0) {
      logger.info({ count: cloudEvents.length }, "Found events via cloud API");
      return cloudEvents;
    }

    // --- Fall back to local HomeBase query via P2P ---
    logger.info("Cloud returned zero events, querying HomeBase local storage via P2P");
    const localEvents = await this.getLocalEvents(deviceSerialNumber, from, to);
    if (localEvents.length > 0) {
      logger.info({ count: localEvents.length }, "Found events on HomeBase local storage");
    } else {
      logger.warn({ deviceSN: deviceSerialNumber }, "No events found via cloud or local query");
    }
    return localEvents;
  }

  private async getCloudEvents(
    deviceSerialNumber: string,
    from: Date,
    to: Date
  ): Promise<EventRecord[]> {
    const api = this.client!.getApi();
    const filter = { deviceSN: deviceSerialNumber };

    let events = await api.getVideoEvents(from, to, filter);
    logger.info({ count: events.length }, "getVideoEvents result");

    if (events.length === 0) {
      events = await api.getHistoryEvents(from, to, filter);
      logger.info({ count: events.length }, "getHistoryEvents result");
    }

    return events.map((e) => ({
      id: `${e.device_sn}_${e.start_time}`,
      deviceSerialNumber: e.device_sn,
      deviceName: e.device_name,
      stationSerialNumber: e.station_sn,
      storagePath: e.storage_path,
      hevcStoragePath: e.hevc_storage_path ?? "",
      cipherId: e.cipher_id,
      startTime: e.start_time,
      endTime: e.end_time,
      thumbPath: e.thumb_path,
      hasHuman: e.has_human === 1,
      videoType: e.video_type,
    }));
  }

  private async getLocalEvents(
    deviceSerialNumber: string,
    from: Date,
    to: Date
  ): Promise<EventRecord[]> {
    const devices = await this.client!.getDevices();
    const device = devices.find((d) => d.getSerial() === deviceSerialNumber);
    if (!device) {
      logger.error({ deviceSN: deviceSerialNumber }, "Device not found");
      return [];
    }

    const stationSN = device.getStationSerial();
    const deviceName = device.getName();

    // Standalone cameras (eufyCam / SoloCam, incl. T8410) are their own station
    // and answer CMD_DATABASE_QUERY_LOCAL with an ACK but never deliver the
    // result event — the P2P session is torn down first. Upstream tracks this as
    // eufy-security-ws#545 and closed it "not planned", so no fix is coming in
    // the library. Attempting it here just burns the full timeout before failing,
    // so bail out immediately with something actionable instead.
    if (Device.isIntegratedDeviceBySn(stationSN)) {
      logger.warn(
        { stationSN, deviceSN: deviceSerialNumber },
        "Skipping local P2P database query: standalone camera does not support it"
      );
      throw new Error(
        `${deviceName} is a standalone camera with no HomeBase, so its local ` +
          "recording index cannot be queried over P2P — eufy never returns a result " +
          "for this command on solo cameras. Downloading by date range is not " +
          "available for this device. Use eufy Cloud event history, or record going " +
          "forward via the live stream."
      );
    }

    // Ensure the station is connected via P2P
    const stationConnected = await this.client!.isStationConnected(stationSN);
    if (!stationConnected) {
      logger.info({ stationSN }, "Connecting to station via P2P...");
      try {
        await this.client!.connectToStation(stationSN);
        logger.info({ stationSN }, "P2P connection established");
      } catch (err) {
        logger.error({ err, stationSN }, "Failed to connect to station via P2P");
        return [];
      }
    }

    const station = await this.client!.getStation(stationSN);

    // databaseQueryLocal is event-based — wrap in a promise.
    //
    // Two independent timeout layers are in play here, and this timer has to
    // be the *longer* one or it masks the library's far more useful verdict:
    //   * databaseQueryLocal only *enqueues* the command. The P2P session keeps
    //     one un-acknowledged command in flight at a time, so this may sit in
    //     the send queue for a while before it is even transmitted.
    //   * only once the station ACKs does the library start its own
    //     MAX_COMMAND_RESULT_WAIT (30s) result timer, and it reports the outcome
    //     on "station command result" — a channel this code did not listen to.
    // Since the library's clock starts at ACK, the old equal 30s app timer
    // always fired first and reported a generic timeout for every distinct
    // underlying cause.
    const records = await new Promise<DatabaseQueryLocal[]>((resolve, reject) => {
      const onDatabaseResult = (
        eventStation: Station,
        returnCode: DatabaseReturnCode,
        data: DatabaseQueryLocal[]
      ) => {
        if (eventStation.getSerial() !== stationSN) return;

        cleanup();
        if (returnCode !== DatabaseReturnCode.SUCCESSFUL) {
          reject(new Error(`Local database query failed (code: ${returnCode})`));
          return;
        }
        resolve(data);
      };

      // Correlate on the command name the library stashes in customData, so an
      // unrelated command timing out on the same station cannot fail this query.
      const onCommandResult = (
        eventStation: Station,
        result: StationCommandResult
      ) => {
        if (eventStation.getSerial() !== stationSN) return;
        if (result?.customData?.command?.name !== CommandName.StationDatabaseQueryLocal) {
          return;
        }
        // This channel fires for successes too (return_code 0) — the actual
        // result arrives on "station database query local". Only a non-zero code
        // means the command itself failed, so ignore the success case here or we
        // would reject every query that worked.
        if (result.return_code === 0) return;

        logger.warn(
          { stationSN, returnCode: result.return_code },
          "P2P layer reported a local database query failure"
        );
        cleanup();
        reject(new Error(describeLocalQueryFailure(result.return_code)));
      };

      const cleanup = () => {
        clearTimeout(timeout);
        this.client!.removeListener("station database query local", onDatabaseResult);
        this.client!.removeListener("station command result", onCommandResult);
      };

      const timeout = setTimeout(() => {
        logger.error({ stationSN }, "Local database query produced no response at all");
        cleanup();
        reject(
          new Error(
            `Local database query timed out after ${
              LOCAL_QUERY_TIMEOUT_MS / 1000
            }s: the HomeBase neither acknowledged nor answered the request. It may ` +
              "be offline, busy, or blocked behind another P2P command."
          )
        );
      }, LOCAL_QUERY_TIMEOUT_MS);

      this.client!.on("station database query local", onDatabaseResult);
      this.client!.on("station command result", onCommandResult);

      logger.info(
        { stationSN, deviceSN: deviceSerialNumber, from: from.toISOString(), to: to.toISOString() },
        "Sending databaseQueryLocal command"
      );

      try {
        station.databaseQueryLocal([deviceSerialNumber], from, to);
      } catch (err) {
        // Throws NotSupportedError when the station model does not implement the
        // command. Surface that directly rather than hanging until the timeout.
        cleanup();
        reject(err as Error);
      }
    });

    logger.info({ count: records.length }, "Local database query returned records");

    // The station only filters at day granularity (it pins start_time to
    // midnight and has no end_time field), so the requested window is not
    // honoured on its own — re-filter here to avoid handing back events from
    // outside the range the user actually asked for.
    const fromSec = Math.trunc(from.getTime() / 1000);
    const toSec = Math.trunc(to.getTime() / 1000);

    return records
      .filter(
        (r) =>
          (r.device_sn === deviceSerialNumber || !r.device_sn) &&
          r.history != null
      )
      .map((r) => {
        const h = r.history;
        return {
          id: `local_${r.record_id}`,
          deviceSerialNumber: r.device_sn ?? deviceSerialNumber,
          deviceName,
          stationSerialNumber: r.station_sn,
          storagePath: h.storage_path,
          hevcStoragePath: "",
          cipherId: h.cipher_id,
          startTime: Math.trunc(h.start_time.getTime() / 1000),
          endTime: Math.trunc(h.end_time.getTime() / 1000),
          thumbPath: h.thumb_path,
          hasHuman: false,
          videoType: h.video_type as number,
        };
      })
      .filter((e) => e.endTime >= fromSec && e.startTime <= toSec);
  }

  async downloadEvent(event: EventRecord, outputPath: string): Promise<void> {
    this.ensureConnected();

    const dir = path.dirname(outputPath);
    if (!fs.existsSync(dir)) {
      fs.mkdirSync(dir, { recursive: true });
    }

    const tempDir = path.join(dir, ".tmp");
    if (!fs.existsSync(tempDir)) {
      fs.mkdirSync(tempDir, { recursive: true });
    }

    const timestamp = Date.now();
    const videoTempPath = path.join(tempDir, `video_${timestamp}.h264`);
    const audioTempPath = path.join(tempDir, `audio_${timestamp}.aac`);

    return new Promise<void>((resolve, reject) => {
      this.activeDownloads.set(event.deviceSerialNumber, {
        resolve,
        reject,
        ffmpeg: null,
        videoTempPath,
        audioTempPath,
        outputPath,
      });

      this.client!.startStationDownload(
        event.deviceSerialNumber,
        event.storagePath,
        event.cipherId
      ).catch((err) => {
        this.activeDownloads.delete(event.deviceSerialNumber);
        this.cleanupTempFiles(videoTempPath, audioTempPath);
        reject(err);
      });

      setTimeout(() => {
        if (this.activeDownloads.has(event.deviceSerialNumber)) {
          this.activeDownloads.delete(event.deviceSerialNumber);
          this.cleanupTempFiles(videoTempPath, audioTempPath);
          reject(new Error("Download timed out after 5 minutes"));
        }
      }, 5 * 60 * 1000);
    });
  }

  async close(): Promise<void> {
    if (this.client) {
      this.savePersistentData();
      this.client.close();
      this.client = null;
      this._status = "disconnected";
      logger.info("Eufy client closed");
    }
  }

  private setupEventListeners(): void {
    if (!this.client) return;

    this.client.on("tfa request", () => {
      this._status = "tfa_required";
      logger.info("2FA verification code required - check your email/SMS");
    });

    this.client.on(
      "captcha request",
      (captchaId: string, captchaImageBase64: string) => {
        this._status = "captcha_required";
        this._captchaInfo = { id: captchaId, imageBase64: captchaImageBase64 };
        logger.info("Captcha verification required");
      }
    );

    this.client.on("connect", () => {
      this._status = "connected";
      this.savePersistentData();
      logger.info("Eufy client connected");
    });

    this.client.on("close", () => {
      if (this._status === "connected") {
        this._status = "disconnected";
        logger.info("Eufy client disconnected");
      }
    });

    this.client.on("connection error", (error: Error) => {
      this._status = "error";
      this._errorMessage = error.message;
      logger.error({ error }, "Eufy connection error");
    });

    this.client.on(
      "station download start",
      (
        _station: Station,
        device: Device,
        metadata: { videoCodec: number; videoFPS: number; videoWidth: number; videoHeight: number },
        videoStream: Readable,
        audioStream: Readable
      ) => {
        const deviceSN = device.getSerial();
        const download = this.activeDownloads.get(deviceSN);
        if (!download) {
          logger.warn(
            { deviceSN },
            "Received download start for unknown device"
          );
          return;
        }

        logger.info(
          {
            deviceSN,
            codec: metadata.videoCodec === 0 ? "H.264" : "H.265",
            resolution: `${metadata.videoWidth}x${metadata.videoHeight}`,
            fps: metadata.videoFPS,
          },
          "Download stream started"
        );

        const videoOut = fs.createWriteStream(download.videoTempPath);
        const audioOut = fs.createWriteStream(download.audioTempPath);

        videoStream.pipe(videoOut);
        audioStream.pipe(audioOut);

        videoStream.on("error", (err) => {
          logger.error({ err, deviceSN }, "Video stream error");
        });
        audioStream.on("error", (err) => {
          logger.error({ err, deviceSN }, "Audio stream error");
        });
      }
    );

    this.client.on(
      "station download finish",
      (_station: Station, device: Device) => {
        const deviceSN = device.getSerial();
        const download = this.activeDownloads.get(deviceSN);
        if (!download) return;

        logger.info({ deviceSN }, "Download stream finished, muxing with FFmpeg");

        setTimeout(() => {
          this.muxWithFfmpeg(download);
        }, 500);
      }
    );
  }

  private muxWithFfmpeg(download: {
    resolve: () => void;
    reject: (err: Error) => void;
    ffmpeg: ChildProcess | null;
    videoTempPath: string;
    audioTempPath: string;
    outputPath: string;
  }): void {
    const videoExists =
      fs.existsSync(download.videoTempPath) &&
      fs.statSync(download.videoTempPath).size > 0;
    const audioExists =
      fs.existsSync(download.audioTempPath) &&
      fs.statSync(download.audioTempPath).size > 0;

    if (!videoExists) {
      this.cleanupTempFiles(download.videoTempPath, download.audioTempPath);
      download.reject(new Error("No video data received"));
      return;
    }

    const args: string[] = ["-y"];

    args.push("-f", "h264", "-i", download.videoTempPath);

    if (audioExists) {
      args.push("-f", "aac", "-i", download.audioTempPath);
      args.push("-map", "0:v", "-map", "1:a");
    }

    args.push("-c:v", "copy");
    if (audioExists) {
      args.push("-c:a", "copy");
    }
    args.push("-movflags", "+faststart", download.outputPath);

    const ffmpeg = spawn("ffmpeg", args);
    download.ffmpeg = ffmpeg;

    let stderrOutput = "";
    ffmpeg.stderr?.on("data", (data: Buffer) => {
      stderrOutput += data.toString();
    });

    ffmpeg.on("close", (code) => {
      this.cleanupTempFiles(download.videoTempPath, download.audioTempPath);

      if (code === 0) {
        logger.info({ outputPath: download.outputPath }, "MP4 file saved");
        download.resolve();
      } else {
        logger.error(
          { code, stderr: stderrOutput.slice(-500) },
          "FFmpeg failed"
        );
        download.reject(new Error(`FFmpeg exited with code ${code}`));
      }
    });

    ffmpeg.on("error", (err) => {
      this.cleanupTempFiles(download.videoTempPath, download.audioTempPath);
      download.reject(
        new Error(`FFmpeg process error: ${err.message}. Is FFmpeg installed?`)
      );
    });
  }

  private cleanupTempFiles(...files: string[]): void {
    for (const f of files) {
      try {
        if (fs.existsSync(f)) fs.unlinkSync(f);
      } catch {
        // Ignore cleanup errors
      }
    }
  }

  private savePersistentData(): void {
    if (!this.client) return;
    try {
      const persistentDir = path.resolve(__dirname, "..", "persistent");
      const sessionPath = path.join(persistentDir, "session.json");
      const data = (this.client as any).getPersistentData?.();
      if (data) {
        fs.writeFileSync(sessionPath, JSON.stringify(data));
      }
    } catch (err) {
      logger.warn({ err }, "Failed to save persistent session data");
    }
  }

  private ensureConnected(): void {
    if (!this.client || this._status !== "connected") {
      throw new Error(
        `Eufy client is not connected (status: ${this._status})`
      );
    }
  }
}
