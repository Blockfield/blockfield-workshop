package blockfield.workshop.ai;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.nio.file.attribute.PosixFilePermissions;
import java.util.Set;

/**
 * Private, bounded rejected content only; never stores HTTP bodies, reasoning or request headers.
 */
final class WorkshopAiRejectedPlans {
    static final int MAX_BYTES = 65_536, FILES = 4;
    private static final Set<String> VALIDATION_MESSAGES =
            Set.of(
                    "Block outside allowed palette",
                    "Client requires older plan language",
                    "Empty operations",
                    "Expected integer",
                    "Expected string",
                    "Expected three coordinates",
                    "Invalid destructive score",
                    "Invalid geometry limits",
                    "Invalid hollow flag",
                    "Invalid material or match",
                    "Invalid tube path",
                    "Invalid tube radius",
                    "Nested repeat",
                    "Operation outside selection",
                    "Plan exceeds block budget",
                    "Plan exceeds expansion work budget",
                    "Plan nesting",
                    "Plan size",
                    "Plan version",
                    "Repeat count",
                    "Repeat offset",
                    "Too many instances",
                    "Too many operations",
                    "Tube outside selection",
                    "Unexpected model tools",
                    "Unknown match block",
                    "Unknown or missing plan field",
                    "Unknown primitive",
                    "Zero-length tube");

    private WorkshopAiRejectedPlans() {}

    static String reason(RuntimeException failure) {
        // Gson/structural exception messages may echo arbitrary model text; only validator
        // literals are safe. Keep the class for every rejected response, even malformed JSON.
        String message = failure.getMessage();
        return failure.getClass().getSimpleName()
                + ": "
                + (VALIDATION_MESSAGES.contains(message == null ? "" : message)
                        ? message
                        : "Malformed plan JSON or structure");
    }

    static synchronized void save(Path directory, String reason, String content, String key)
            throws IOException {
        if (Files.isSymbolicLink(directory)) throw new IOException("Debug directory is a link");
        Files.createDirectories(directory);
        boolean posix = Files.getFileStore(directory).supportsFileAttributeView("posix");
        if (posix)
            Files.setPosixFilePermissions(directory, PosixFilePermissions.fromString("rwx------"));
        String safe = key.isEmpty() ? content : content.replace(key, "[REDACTED]");
        safe = safe.replaceAll("(?i)(bearer\\s+)[^\\s\"]+", "$1[REDACTED]");
        boolean truncated = safe.getBytes(StandardCharsets.UTF_8).length > MAX_BYTES - 512;
        // Four UTF-8 bytes per UTF-16 code unit is a conservative bound, including the header.
        if (truncated) safe = safe.substring(0, Math.min(safe.length(), (MAX_BYTES - 512) / 4));
        String text = reason + "\ntruncated=" + truncated + "\n" + safe;
        if (text.getBytes(StandardCharsets.UTF_8).length > MAX_BYTES)
            throw new IOException("Debug size limit");
        for (int i = FILES - 1; i > 0; i--) {
            Path old = directory.resolve("rejected-" + (i - 1) + ".txt");
            Path next = directory.resolve("rejected-" + i + ".txt");
            if (Files.isSymbolicLink(old) || Files.isSymbolicLink(next))
                throw new IOException("Debug file is a link");
            if (Files.exists(old))
                Files.move(old, next, java.nio.file.StandardCopyOption.REPLACE_EXISTING);
        }
        Path current = directory.resolve("rejected-0.txt");
        try (var channel =
                Files.newByteChannel(
                        current,
                        Set.of(
                                StandardOpenOption.CREATE,
                                StandardOpenOption.TRUNCATE_EXISTING,
                                StandardOpenOption.WRITE,
                                LinkOption.NOFOLLOW_LINKS),
                        posix
                                ? new java.nio.file.attribute.FileAttribute<?>[] {
                                    PosixFilePermissions.asFileAttribute(
                                            PosixFilePermissions.fromString("rw-------"))
                                }
                                : new java.nio.file.attribute.FileAttribute<?>[0])) {
            var bytes = java.nio.ByteBuffer.wrap(text.getBytes(StandardCharsets.UTF_8));
            while (bytes.hasRemaining()) channel.write(bytes);
        }
        if (posix)
            Files.setPosixFilePermissions(current, PosixFilePermissions.fromString("rw-------"));
    }
}
