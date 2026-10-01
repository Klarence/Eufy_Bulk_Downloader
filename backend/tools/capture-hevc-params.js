/**
 * Capture the camera's authentic HEVC VPS/SPS/PPS from its live P2P stream.
 *
 * Why this exists: the .zxvideo clips on the microSD card are 4K (3840x2160)
 * and ship with their parameter sets stripped. Without them the slices cannot
 * be decoded at all, and no amount of guessing at encoder settings produces a
 * bit-exact match. The camera sends real parameter sets in-band at the start of
 * every live stream, so read them from there.
 *
 * The live stream must use the SAME resolution as the SD recordings (set the
 * app to Ultra/4K) or the captured sets will not describe the card data.
 *
 *   node tools/capture-hevc-params.js [--device T8417...] [--out ../tools/hevc_headers_4k.bin]
 */
require("dotenv").config({ path: require("path").join(__dirname, "..", ".env") });

const fs = require("fs");
const path = require("path");
const { EufySecurity, VideoCodec } = require("eufy-security-client");

const argv = process.argv.slice(2);
const arg = (name, dflt) => {
  const i = argv.indexOf(`--${name}`);
  return i !== -1 && argv[i + 1] ? argv[i + 1] : dflt;
};

const DEVICE_FILTER = arg("device", "T8417");
const OUT = path.resolve(
  __dirname,
  "..",
  "..",
  arg("out", "tools/hevc_headers_T8417_4k.bin")
);
const WANT_BYTES = 256 * 1024;
const STREAM_TIMEOUT_MS = 45_000;

/** Pull the leading VPS/SPS/PPS out of an Annex-B stream, SEI excluded. */
function extractParamSets(buf) {
  const nals = [];
  for (let j = 0; j < buf.length - 4; ) {
    const k = buf.indexOf(Buffer.from([0, 0, 1]), j);
    if (k === -1) break;
    const start = k > 0 && buf[k - 1] === 0 ? k - 1 : k;
    if (k + 3 < buf.length) nals.push({ start, type: (buf[k + 3] >> 1) & 0x3f });
    j = k + 3;
  }
  const out = [];
  for (let i = 0; i < nals.length && nals[i].type >= 32 && nals[i].type <= 34; i++) {
    const end = i + 1 < nals.length ? nals[i + 1].start : nals[i].start + 64;
    out.push({ type: nals[i].type, bytes: buf.subarray(nals[i].start, end) });
  }
  return { sets: out, nals };
}

async function main() {
  const email = process.env.EUFY_EMAIL;
  const password = process.env.EUFY_PASSWORD;
  if (!email || !password) {
    console.error("EUFY_EMAIL / EUUFY_PASSWORD not found in backend/.env");
    process.exit(1);
  }

  // The library reads/writes persistent/persistent.json from persistentDir on
  // its own, so pointing at the backend's folder reuses the existing session
  // and avoids a fresh login (and any 2FA / captcha prompt).
  const persistentDir = path.resolve(__dirname, "..", "persistent");
  fs.mkdirSync(persistentDir, { recursive: true });

  const config = {
    username: email,
    password,
    country: process.env.EUFY_COUNTRY || "US",
    language: "en",
    persistentDir,
    p2pConnectionSetup: 0,
    pollingIntervalMinutes: 10,
    eventDurationSeconds: 10,
  };

  // EufySecurity must be built through the static async factory; `new` leaves
  // the internal API undefined and connect() then throws on this.api.login.
  const client = await EufySecurity.initialize(config);

  // connect() swallows login failures into this logger, so raise the main
  // category to Warn - otherwise a rejected password looks like a silent no-op.
  // LogLevel is a numeric enum here (3 === "Warn"), not a name-keyed object.
  try {
    client.setLoggingLevel(0, 3);
  } catch (e) {
    /* logging categories are version-specific; not fatal */
  }

  // connect() has no return statement: it awaits login and swallows any error
  // into its own log, so it always resolves to undefined. Checking the return
  // value is meaningless. The backend instead watches status events, and the
  // real proof of a working session is being able to list devices.
  let connectError = null;
  let needsTfa = false;
  let needsCaptcha = false;
  client.on("tfa request", () => { needsTfa = true; });
  client.on("captcha request", () => { needsCaptcha = true; });
  client.on("captcha required", () => { needsCaptcha = true; });
  client.on("connection error", (e) => { connectError = e; });
  client.on("connect", () => console.log("  [event] connect"));

  console.log("connecting to eufy cloud...");
  await client.connect();

  if (needsTfa) {
    console.error("account requires 2FA - open the running backend and sign in there first,");
    console.error("then re-run this script so the saved session is fresh.");
    process.exit(1);
  }
  if (needsCaptcha) {
    console.error("account requires captcha - sign in via the running backend first.");
    process.exit(1);
  }
  if (connectError) {
    console.error("connection error:", connectError.message || connectError);
    process.exit(1);
  }

  let devices = [];
  try {
    devices = await client.getDevices();
  } catch (e) {
    console.error("login did not yield a usable session:", e.message);
    process.exit(1);
  }
  if (!devices.length) {
    console.error("connected but no devices returned - session is not usable.");
    process.exit(1);
  }
  console.log(`session OK, ${devices.length} device(s) visible`);
  const device = devices.find(
    (d) => d.getSerial().startsWith(DEVICE_FILTER) ||
           (d.getName() || "").toUpperCase().includes("T8417")
  );
  if (!device) {
    console.error(`no device matching "${DEVICE_FILTER}"`);
    console.error("devices:", devices.map((d) => `${d.getSerial()} (${d.getName()})`));
    process.exit(1);
  }

  const stationSN = device.getStationSerial();
  console.log(`device ${device.getSerial()} "${device.getName()}" -> station ${stationSN}`);

  if (!(await client.isStationConnected(stationSN))) {
    console.log("connecting to station over P2P...");
    await client.connectToStation(stationSN);
  }

  const station = await client.getStation(stationSN);
  console.log("software version:", station.getSoftwareVersion());

  // The T8417 reports device_type 105 (INDOOR_PT_CAMERA_E30), which is absent
  // from eufy-security-client 3.8.0's DeviceCommands table. hasCommand() is
  // just `getCommands().includes(name)`, so an unknown type denies *every*
  // command and startLivestream throws NotSupportedError. That is a metadata
  // gap in the library, not a hardware limit - patch the lookup so we can ask
  // the camera directly instead of trusting the table.
  const origHasCommand = device.hasCommand.bind(device);
  device.hasCommand = (name) =>
    origHasCommand(name) || name === "deviceStartLivestream";
  console.log(
    "device type:",
    device.getDeviceType(),
    "- patching hasCommand (unknown device type in library table)"
  );

  const done = new Promise((resolve, reject) => {
    const timer = setTimeout(
      () => reject(new Error(`no livestream data within ${STREAM_TIMEOUT_MS / 1000}s`)),
      STREAM_TIMEOUT_MS
    );

    client.on("station livestream start", async (st, dev, metadata, videoStream) => {
      console.log("livestream start, metadata:", {
        codec: metadata?.videoCodec,
        width: metadata?.videoWidth,
        height: metadata?.videoHeight,
        fps: metadata?.videoFPS,
        type: metadata?.videoType,
      });

      const chunks = [];
      let got = 0;
      try {
        await new Promise((res, rej) => {
          const onData = (c) => {
            chunks.push(Buffer.from(c));
            got += c.length;
            if (got >= WANT_BYTES) {
              cleanup();
              res();
            }
          };
          const onEnd = () => { cleanup(); res(); };
          const onErr = (e) => { cleanup(); rej(e); };
          const cleanup = () => {
            videoStream.off("data", onData);
            videoStream.off("end", onEnd);
            videoStream.off("error", onErr);
            clearTimeout(timer);
          };
          videoStream.on("data", onData);
          videoStream.on("end", onEnd);
          videoStream.on("error", onErr);
          setTimeout(() => { cleanup(); res(); }, 12_000);
        });
      } catch (e) {
        console.error("stream read error:", e.message);
      }

      const buf = Buffer.concat(chunks);
      console.log(`captured ${buf.length} bytes of video stream`);
      if (buf.length < 1024) {
        reject(new Error("captured too little data"));
        return;
      }
      const { sets, nals } = extractParamSets(buf);
      console.log(
        "leading NAL types:",
        nals.slice(0, 8).map((n) => n.type).join(",")
      );
      if (!sets.length) {
        reject(new Error("no VPS/SPS/PPS at the head of the stream"));
        return;
      }
      const names = { 32: "VPS", 33: "SPS", 34: "PPS" };
      const blob = Buffer.concat(sets.map((s) => s.bytes));
      fs.mkdirSync(path.dirname(OUT), { recursive: true });
      fs.writeFileSync(OUT, blob);
      console.log(
        `wrote ${OUT} (${blob.length} bytes: ${sets
          .map((s) => `${names[s.type]}=${s.bytes.length}B`)
          .join(", ")})`
      );
      resolve({ metadata, blob });
    });

    client.on("station livestream error", (st, dev, err) => {
      console.error("livestream error:", err?.message || err);
    });
  });

  console.log("starting livestream (HEVC)...");
  let started = false;
  try {
    station.startLivestream(device, VideoCodec.H265);
    started = true;
  } catch (e) {
    // HEVC is what we need, but fall back to H264 purely as a probe: if that
    // also fails the camera is refusing livestream outright, and if it works
    // we at least learn the stream path is usable.
    console.warn(`HEVC livestream refused (${e.message}); probing with H264...`);
    try {
      station.startLivestream(device, VideoCodec.H264);
      started = true;
      console.warn("NOTE: only H264 was accepted. H264 parameter sets are useless for");
      console.warn("      decoding HEVC SD clips - this run will not produce usable headers.");
    } catch (e2) {
      console.error("livestream refused in both codecs:", e2.message);
      process.exit(1);
    }
  }
  if (!started) process.exit(1);

  try {
    const { metadata, blob } = await done;
    console.log("\n--- next step ---");
    console.log(`stream reported ${metadata?.videoWidth}x${metadata?.videoHeight}`);
    if (metadata?.videoWidth !== 3840 || metadata?.videoHeight !== 2160) {
      console.warn(
        "WARNING: stream is not 3840x2160. Its parameter sets will NOT match the\n" +
        "         4K SD recordings. Raise the app's stream quality, or this file\n" +
        "         will still produce corrupt output."
      );
    }
    console.log(`verify with: python3 tools/verify-headers.py ${OUT}`);
    process.exit(blob.length ? 0 : 1);
  } catch (e) {
    console.error("FAILED:", e.message);
    process.exit(1);
  }
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
