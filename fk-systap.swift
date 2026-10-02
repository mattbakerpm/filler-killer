// fk-systap — reports how loud the Mac's own playback is, for the speaker gate.
//
// Filler Killer captures the mic plainly (macOS voice processing mutes the mic
// for other apps, e.g. a Teams/Meet call in Chrome). To still ignore the other
// people on a speakerphone call, it needs to know WHEN the Mac is playing
// audio. This helper taps all system output with a Core Audio process tap
// (macOS 14.2+, "System Audio Recording" permission) and prints one line per
// ~50 ms to stdout:
//
//     ready <sample-rate> <channels>      once, after the tap starts
//     <dBFS>                              e.g. -34.2  (-120.0 = silence)
//
// Exits when stdin closes (the parent app quit) or when the default output
// device changes (the parent restarts it against the new device). Audio is
// only measured, never stored or sent anywhere.
import AudioToolbox
import CoreAudio
import Foundation

setvbuf(stdout, nil, _IOLBF, 0)

func fail(_ msg: String, _ code: Int32 = 1) -> Never {
    FileHandle.standardError.write("fk-systap: \(msg)\n".data(using: .utf8)!)
    exit(code)
}

func sysAddress(_ sel: AudioObjectPropertySelector) -> AudioObjectPropertyAddress {
    AudioObjectPropertyAddress(mSelector: sel,
                               mScope: kAudioObjectPropertyScopeGlobal,
                               mElement: kAudioObjectPropertyElementMain)
}

func defaultOutputDevice() -> AudioObjectID {
    var addr = sysAddress(kAudioHardwarePropertyDefaultSystemOutputDevice)
    var dev = AudioObjectID(kAudioObjectUnknown)
    var size = UInt32(MemoryLayout<AudioObjectID>.size)
    let err = AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject),
                                         &addr, 0, nil, &size, &dev)
    if err != noErr || dev == kAudioObjectUnknown { fail("no output device (\(err))") }
    return dev
}

func deviceUID(_ dev: AudioObjectID) -> String {
    var addr = sysAddress(kAudioDevicePropertyDeviceUID)
    var uid: Unmanaged<CFString>?
    var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    let err = AudioObjectGetPropertyData(dev, &addr, 0, nil, &size, &uid)
    guard err == noErr, let u = uid else { fail("no device UID (\(err))") }
    return u.takeRetainedValue() as String
}

// --- 1. a private, unmuted tap of everything the Mac plays ---
let tapDesc = CATapDescription(stereoGlobalTapButExcludeProcesses: [])
tapDesc.uuid = UUID()
tapDesc.name = "Filler Killer speaker gate"
tapDesc.isPrivate = true
tapDesc.muteBehavior = .unmuted
var tapID = AudioObjectID(kAudioObjectUnknown)
var err = AudioHardwareCreateProcessTap(tapDesc, &tapID)
if err != noErr { fail("process tap failed (\(err)) — needs macOS 14.2+", 2) }

var aggID = AudioObjectID(kAudioObjectUnknown)
var procID: AudioDeviceIOProcID?

func cleanup() {
    if let p = procID {
        AudioDeviceStop(aggID, p)
        AudioDeviceDestroyIOProcID(aggID, p)
        procID = nil
    }
    if aggID != kAudioObjectUnknown {
        AudioHardwareDestroyAggregateDevice(aggID)
        aggID = AudioObjectID(kAudioObjectUnknown)
    }
    if tapID != kAudioObjectUnknown {
        AudioHardwareDestroyProcessTap(tapID)
        tapID = AudioObjectID(kAudioObjectUnknown)
    }
}

// --- 2. a private aggregate device that carries the tap (clocked by the output) ---
let outDev = defaultOutputDevice()
let outUID = deviceUID(outDev)
let aggDesc: [String: Any] = [
    kAudioAggregateDeviceNameKey: "Filler Killer speaker gate",
    kAudioAggregateDeviceUIDKey: UUID().uuidString,
    kAudioAggregateDeviceMainSubDeviceKey: outUID,
    kAudioAggregateDeviceIsPrivateKey: true,
    kAudioAggregateDeviceIsStackedKey: false,
    kAudioAggregateDeviceTapAutoStartKey: true,
    kAudioAggregateDeviceSubDeviceListKey: [[kAudioSubDeviceUIDKey: outUID]],
    kAudioAggregateDeviceTapListKey: [[
        kAudioSubTapDriftCompensationKey: true,
        kAudioSubTapUIDKey: tapDesc.uuid.uuidString,
    ]],
]
err = AudioHardwareCreateAggregateDevice(aggDesc as CFDictionary, &aggID)
if err != noErr { cleanup(); fail("aggregate device failed (\(err))") }

var fmtAddr = sysAddress(kAudioTapPropertyFormat)
var asbd = AudioStreamBasicDescription()
var asbdSize = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
err = AudioObjectGetPropertyData(tapID, &fmtAddr, 0, nil, &asbdSize, &asbd)
if err != noErr { cleanup(); fail("tap format unavailable (\(err))") }
if asbd.mFormatID != kAudioFormatLinearPCM || asbd.mFormatFlags & kAudioFormatFlagIsFloat == 0
    || asbd.mBitsPerChannel != 32 {
    cleanup(); fail("unexpected tap format (bits \(asbd.mBitsPerChannel))")
}

// --- 3. measure RMS per ~50 ms window, print dBFS off the audio thread ---
let printQ = DispatchQueue(label: "fk-systap.print")
let window = max(1, Int(asbd.mSampleRate * 0.05))
var sumSq: Double = 0
var frames = 0

err = AudioDeviceCreateIOProcIDWithBlock(&procID, aggID, nil) { _, inData, _, _, _ in
    let abl = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: inData))
    var n = 0
    var s: Double = 0
    for buf in abl {
        guard let data = buf.mData else { continue }
        let count = Int(buf.mDataByteSize) / MemoryLayout<Float>.size
        let p = data.assumingMemoryBound(to: Float.self)
        for i in 0..<count { let v = Double(p[i]); s += v * v }
        n += count
    }
    let ch = max(1, Int(asbd.mChannelsPerFrame))
    sumSq += s
    frames += n / ch
    if frames >= window {
        let rms = (sumSq / Double(max(1, frames * ch))).squareRoot()
        let db = rms > 1e-6 ? 20 * log10(rms) : -120.0
        sumSq = 0
        frames = 0
        printQ.async { print(String(format: "%.1f", db)) }
    }
}
if err != noErr { cleanup(); fail("IOProc failed (\(err))") }
err = AudioDeviceStart(aggID, procID)
if err != noErr { cleanup(); fail("start failed (\(err))") }
printQ.async { print("ready \(Int(asbd.mSampleRate)) \(asbd.mChannelsPerFrame)") }

// --- 4. lifetime: parent gone (stdin EOF), signal, or output device switch ---
for sig in [SIGTERM, SIGINT, SIGHUP] {
    signal(sig, SIG_IGN)
    let src = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    src.setEventHandler { cleanup(); exit(0) }
    src.resume()
    _ = Unmanaged.passRetained(src)  // keep alive for the process lifetime
}
DispatchQueue.global().async {
    var b = [UInt8](repeating: 0, count: 256)
    while read(0, &b, b.count) > 0 {}
    DispatchQueue.main.async { cleanup(); exit(0) }
}
var devAddr = sysAddress(kAudioHardwarePropertyDefaultSystemOutputDevice)
AudioObjectAddPropertyListenerBlock(AudioObjectID(kAudioObjectSystemObject), &devAddr, .main) { _, _ in
    // the aggregate is clocked by the old device; let the parent restart us
    cleanup()
    exit(3)
}
dispatchMain()
