/**
 * Probe whether eufy's own cloud/P2P path can retrieve the later (1080p) clips,
 * rather than reverse-engineering their container from the raw bytes.
 *
 * The 4K era (Jan-Mar) is already extracted. These later files use a different
 * per-frame container that ffmpeg cannot read. eufy's app clearly can play
 * them, so the question is whether eufy-security-client exposes that path.
 *
 *   node tools/probe-event-path.js
 */
require("dotenv").config({ path: require("path").join(__dirname, "..", ".env") });

const path = require("path");
const fs = require("fs");
const { EufySecurity } = require("eufy-security-client");

const OUT = path.resolve(__dirname, "..", "..", "cloud-probe.json");

async function main() {
  const persistentDir = path.resolve(__dirname, "..", "persistent");
  const client = await EufySecurity.initialize({
    username: process.env.EUFY_EMAIL,
    password: process.env.EUFY_PASSWORD,
    country: process.env.EUFY_COUNTRY || "US",
    language: "en",
    persistentDir,
    p2pConnectionSetup: 0,
  });

  let connErr = null;
  client.on("connection error", (e) => { connErr = e; });
  await client.connect();
  if (connErr) { console.error("connect error:", connErr.message); process.exit(1); }

  const api = client.getApi();
  const device = (await client.getDevices())[0];
  const dsn = device.getSerial();
  console.log("device:", dsn, "|", device.getName());

  // 1. What does the cloud know about, in total?
  let all = [];
  try {
    all = await api.getAllVideoEvents();
    console.log(`\ncloud getAllVideoEvents(): ${all.length} events`);
  } catch (e) {
    console.log("getAllVideoEvents failed:", e.message);
  }

  // 2. Narrow query around the first later-era clip.
  const from = new Date("2026-05-29T00:00:00Z");
  const to = new Date("2026-06-02T00:00:00Z");
  for (const [name, fn] of [["getVideoEvents", "getVideoEvents"], ["getHistoryEvents", "getHistoryEvents"]]) {
    try {
      const ev = await api[fn](from, to, { deviceSN: dsn });
      console.log(`${name}(2026-05-29..06-02): ${ev.length} events`);
      if (ev.length) {
        const e0 = ev[0];
        console.log("  sample:", JSON.stringify({
          start_time: e0.start_time, storage_type: e0.storage_type,
          cloud_path: e0.cloud_path, storage_path: e0.storage_path,
          cipher_id: e0.cipher_id, res: [e0.res_best_width, e0.res_best_height],
          frame_num: e0.frame_num, video_type: e0.video_type,
        }, null, 2));
      }
    } catch (err) {
      console.log(`${name} failed:`, err.message);
    }
  }

  // 3. Can the library pull a file from the station itself?
  const station = await client.getStation(device.getStationSerial());
  if (!(await client.isStationConnected(device.getStationSerial()))) {
    console.log("\nconnecting to station...");
    await client.connectToStation(device.getStationSerial());
  }
  const target = "/media/mmcblk0p1/Camera00/event/202605/20260530/20260530200842.zxvideo";
  console.log("\nstation software:", station.getSoftwareVersion());
  for (const codec of [undefined]) {
    try {
      const t = setTimeout(() => console.log("  (startDownload did not settle in 20s)"), 20000);
      await station.startDownload(device, target, 0);
      clearTimeout(t);
      console.log("startDownload returned");
    } catch (e) {
      console.log("startDownload failed:", e.message);
    }
  }

  fs.writeFileSync(OUT, JSON.stringify({ count: all.length, sample: all.slice(0, 3) }, null, 2));
  console.log("\nwrote", OUT);

  // Keep the connection alive briefly in case a download event fires.
  setTimeout(() => process.exit(0), 5000);
}

main().catch((e) => { console.error(e); process.exit(1); });