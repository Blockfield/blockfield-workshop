package blockfield.workshop.ai;

import static org.junit.jupiter.api.Assertions.*;

import org.junit.jupiter.api.Test;

import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.util.List;

/** STT input bound: rounds partial seconds up from a validated WAV, any well-shaped model ID. */
class WorkshopAiAudioTest {
    private static byte[] wav() {
        return wav(4);
    }

    private static byte[] wav(int pcmBytes) {
        var b = ByteBuffer.allocate(44 + pcmBytes).order(ByteOrder.LITTLE_ENDIAN);
        b.putInt(0x46464952)
                .putInt(b.capacity() - 8)
                .putInt(0x45564157)
                .putInt(0x20746d66)
                .putInt(16)
                .putShort((short) 1)
                .putShort((short) 1)
                .putInt(16000)
                .putInt(32000)
                .putShort((short) 2)
                .putShort((short) 16)
                .putInt(0x61746164)
                .putInt(pcmBytes);
        return b.array();
    }

    @Test
    void durationReserveRoundsUpPartialSecondsFromValidatedPcm() {
        int[][] cases = {
            {2, 100},
            {31998, 100},
            {32000, 100},
            {32002, 200},
            {255998, 800},
            {256000, 800},
            {256002, 900},
            {959998, 3000},
            {960000, 3000}
        };
        for (int[] entry : cases)
            assertEquals(
                    entry[1], GenerateFlow.transcriptionBound(wav(entry[0]), "openai/whisper-1"));
        assertThrows(
                IllegalArgumentException.class,
                () -> GenerateFlow.transcriptionBound(wav(960002), "openai/whisper-1"));
        byte[] forged = wav(32002);
        ByteBuffer.wrap(forged).order(ByteOrder.LITTLE_ENDIAN).putInt(40, 2);
        assertThrows(
                IllegalArgumentException.class,
                () -> GenerateFlow.transcriptionBound(forged, "openai/whisper-1"));
        assertEquals(100, GenerateFlow.transcriptionBound(wav(), "openai/whisper-large-v3"));
        assertEquals(100, GenerateFlow.transcriptionBound(wav(), "no-vendor"));
        for (String model : List.of("", "a/" + "b".repeat(127)))
            assertThrows(
                    IllegalArgumentException.class,
                    () -> GenerateFlow.transcriptionBound(wav(), model));
    }
}
