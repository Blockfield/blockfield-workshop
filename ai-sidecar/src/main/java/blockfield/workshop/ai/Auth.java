package blockfield.workshop.ai;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.util.regex.Pattern;

/** Bearer secret for mod -> sidecar calls (contract "Auth"). */
final class Auth {
    static final Pattern KEY_PATTERN = Pattern.compile("[A-Za-z0-9_-]{20,2048}");

    private Auth() {}

    /** Reads and validates the key file; fails closed on any problem. */
    static String loadKey(Path file) throws IOException {
        String key = Files.readString(file, StandardCharsets.UTF_8).strip();
        if (!KEY_PATTERN.matcher(key).matches()) {
            throw new IOException("invalid sidecar key file: " + file);
        }
        return key;
    }

    static boolean authorized(String header, String secret) {
        if (header == null || !header.startsWith("Bearer ")) {
            return false;
        }
        byte[] want = ("Bearer " + secret).getBytes(StandardCharsets.UTF_8);
        byte[] got = header.getBytes(StandardCharsets.UTF_8);
        return MessageDigest.isEqual(want, got);
    }
}
