package blockfield.workshop.ai;

import java.nio.ByteBuffer;
import java.nio.ByteOrder;

/**
 * Bounds and shape check for the 16 kHz mono PCM16 WAV the transcribe endpoint accepts. The game
 * assembles chunked uploads and validates with the same bounds; the sidecar only needs the final
 * validation.
 */
final class WorkshopAiAudio {
    static final int MAX_AUDIO_BYTES = 30 * 16_000 * 2 + 44;

    private WorkshopAiAudio() {}

    static void validate(byte[] wav) {
        if (wav.length < 46 || wav.length > MAX_AUDIO_BYTES || (wav.length - 44) % 2 != 0)
            throw new IllegalArgumentException("Invalid PCM length");
        var buffer = ByteBuffer.wrap(wav).order(ByteOrder.LITTLE_ENDIAN);
        if (buffer.getInt(0) != 0x46464952
                || buffer.getInt(4) != wav.length - 8
                || buffer.getInt(8) != 0x45564157
                || buffer.getInt(12) != 0x20746d66
                || buffer.getInt(16) != 16
                || buffer.getShort(20) != 1
                || buffer.getShort(22) != 1
                || buffer.getInt(24) != 16000
                || buffer.getInt(28) != 32000
                || buffer.getShort(32) != 2
                || buffer.getShort(34) != 16
                || buffer.getInt(36) != 0x61746164
                || buffer.getInt(40) != wav.length - 44)
            throw new IllegalArgumentException("Expected bounded mono PCM16 WAV at 16kHz");
    }
}
