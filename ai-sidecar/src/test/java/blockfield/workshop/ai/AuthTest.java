package blockfield.workshop.ai;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;

class AuthTest {
    @TempDir Path dir;

    private Path keyFile(String content) throws IOException {
        Path file = dir.resolve("ai-sidecar.key");
        Files.writeString(file, content, StandardCharsets.UTF_8);
        return file;
    }

    @Test
    void loadsValidKey() throws IOException {
        String secret = "Abcdefghij0123456789-_ABCDEF";
        assertEquals(secret, Auth.loadKey(keyFile(secret)));
    }

    @Test
    void stripsTrailingNewline() throws IOException {
        String secret = "Abcdefghij0123456789-_ABCDEF";
        assertEquals(secret, Auth.loadKey(keyFile(secret + "\n")));
    }

    @Test
    void rejectsShortKey() throws IOException {
        Path file = keyFile("too-short");
        try {
            Auth.loadKey(file);
            assertFalse(true, "expected IOException");
        } catch (IOException expected) {
            assertTrue(expected.getMessage().contains("invalid sidecar key"));
        }
    }

    @Test
    void rejectsWhitespaceInside() throws IOException {
        Path file = keyFile("Abcdefghij0123456789 with-space_ABCDEF");
        try {
            Auth.loadKey(file);
            assertFalse(true, "expected IOException");
        } catch (IOException expected) {
            assertTrue(expected.getMessage().contains("invalid sidecar key"));
        }
    }

    @Test
    void missingFileThrows() {
        try {
            Auth.loadKey(dir.resolve("absent.key"));
            assertFalse(true, "expected IOException");
        } catch (IOException expected) {
            // Fail closed: no key file means no startup.
        }
    }

    @Test
    void bearerCheck() {
        String secret = "Abcdefghij0123456789-_ABCDEF";
        assertTrue(Auth.authorized("Bearer " + secret, secret));
        assertFalse(Auth.authorized("Bearer wrong-secret-value-0123456789", secret));
        assertFalse(Auth.authorized("Basic " + secret, secret));
        assertFalse(Auth.authorized(null, secret));
    }
}
