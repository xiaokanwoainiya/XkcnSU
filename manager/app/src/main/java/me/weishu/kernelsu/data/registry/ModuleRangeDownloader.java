package me.weishu.kernelsu.data.registry;

import java.io.Closeable;
import java.io.EOFException;
import java.io.File;
import java.io.IOException;
import java.io.InputStream;
import java.io.InterruptedIOException;
import java.io.RandomAccessFile;
import java.nio.ByteBuffer;
import java.nio.channels.FileChannel;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.ExecutorCompletionService;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicLong;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/** The range probe and Content-Range checks come from Chunian Assistant's download code. */
public final class ModuleRangeDownloader {
    private static final int CONNECTIONS = 16;
    private static final long MIN_BYTES = 1024L * 1024L;
    private static final Pattern CONTENT_RANGE =
            Pattern.compile("bytes\\s+(\\d+)-(\\d+)/(\\d+)", Pattern.CASE_INSENSITIVE);

    public interface Transport {
        RangeCall newCall(String range, String ifRange);
    }

    public interface RangeCall {
        Reply execute() throws IOException;
        void cancel();
    }

    public interface Progress {
        void update(long bytes, long total);
    }

    public static final class Reply implements Closeable {
        private final int status;
        private final long contentLength;
        private final String contentRange;
        private final String contentEncoding;
        private final String etag;
        private final InputStream body;
        private final Closeable response;

        public Reply(int status, long contentLength, String contentRange, String contentEncoding,
                     String etag, InputStream body, Closeable response) {
            this.status = status;
            this.contentLength = contentLength;
            this.contentRange = contentRange;
            this.contentEncoding = contentEncoding;
            this.etag = etag;
            this.body = body;
            this.response = response;
        }

        @Override
        public void close() throws IOException {
            response.close();
        }
    }

    private interface ReadReply<T> {
        T read(Reply response) throws IOException;
    }

    private static final class Source {
        private final long length;
        private final String etag;

        private Source(long length, String etag) {
            this.length = length;
            this.etag = etag;
        }
    }

    private final Transport transport;
    private final Set<RangeCall> calls = new HashSet<>();
    private volatile boolean cancelled;

    public ModuleRangeDownloader(Transport transport) {
        this.transport = transport;
    }

    /** Stops this attempt, including calls that have not received response headers yet. */
    public void cancel() {
        List<RangeCall> snapshot;
        synchronized (calls) {
            cancelled = true;
            snapshot = new ArrayList<>(calls);
        }
        for (RangeCall call : snapshot) {
            call.cancel();
        }
    }

    /**
     * Fills an unpublished temporary file. Returns false if the source should be streamed
     * instead. A negative expected size means the download has no registry digest.
     */
    public boolean download(File temporary, long expectedSize, Progress progress) throws IOException {
        if (expectedSize >= 0 && expectedSize < MIN_BYTES) return false;
        Source source = request("bytes=0-0", null, reply -> {
            if (reply.status != 206) return null;
            long total = validateRange(reply, 0, 1, -1);
            if (reply.body.read() == -1 || reply.body.read() != -1) {
                throw new IOException("Invalid range probe length");
            }
            return new Source(total, strongEtag(reply.etag));
        });
        if (source == null || source.length < MIN_BYTES ||
                (expectedSize >= 0 && source.length != expectedSize) ||
                (expectedSize < 0 && source.etag == null)) {
            return false;
        }

        try (RandomAccessFile output = new RandomAccessFile(temporary, "rw")) {
            output.setLength(0);
            FileChannel channel = output.getChannel();
            AtomicLong bytes = new AtomicLong();
            long block = source.length / CONNECTIONS;
            long extra = source.length % CONNECTIONS;
            ExecutorService workers = Executors.newFixedThreadPool(CONNECTIONS);
            try {
                ExecutorCompletionService<Void> completed = new ExecutorCompletionService<>(workers);
                for (int index = 0; index < CONNECTIONS; index++) {
                    long start = block * index + Math.min(index, extra);
                    long count = block + (index < extra ? 1 : 0);
                    completed.submit(() -> {
                        copyRange(channel, source, start, count, bytes, progress);
                        return null;
                    });
                }
                for (int index = 0; index < CONNECTIONS; index++) {
                    completed.take().get();
                }
                checkActive();
                if (bytes.get() != source.length || output.length() != source.length) {
                    throw new IOException("Assembled download length does not match");
                }
                output.getFD().sync();
                return true;
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                throw new InterruptedIOException("Module download interrupted");
            } catch (ExecutionException e) {
                if (e.getCause() instanceof IOException) throw (IOException) e.getCause();
                throw new IOException("Module download worker failed", e.getCause());
            } finally {
                cancel();
                workers.shutdownNow();
            }
        }
    }

    private void copyRange(FileChannel output, Source source, long start, long count,
                           AtomicLong bytes, Progress progress) throws IOException {
        String range = "bytes=" + start + "-" + (start + count - 1);
        request(range, source.etag, reply -> {
            validateRange(reply, start, count, source.length);
            if (source.etag != null && !source.etag.equals(strongEtag(reply.etag))) {
                throw new IOException("Module changed during range download");
            }
            byte[] buffer = new byte[64 * 1024];
            long offset = start;
            long remaining = count;
            while (remaining > 0) {
                checkActive();
                int read = reply.body.read(buffer, 0, (int) Math.min(buffer.length, remaining));
                if (read <= 0) throw new EOFException("Range response ended early");
                ByteBuffer chunk = ByteBuffer.wrap(buffer, 0, read);
                while (chunk.hasRemaining()) {
                    int written = output.write(chunk, offset);
                    if (written <= 0) throw new IOException("Could not write module range");
                    offset += written;
                }
                remaining -= read;
                checkActive();
                progress.update(bytes.addAndGet(read), source.length);
            }
            if (reply.body.read() != -1) throw new IOException("Range response is too long");
            return null;
        });
    }

    private <T> T request(String range, String ifRange, ReadReply<T> reader) throws IOException {
        RangeCall call = transport.newCall(range, ifRange);
        synchronized (calls) {
            checkActive();
            calls.add(call);
        }
        try (Reply reply = call.execute()) {
            checkActive();
            return reader.read(reply);
        } finally {
            synchronized (calls) {
                calls.remove(call);
            }
        }
    }

    private void checkActive() throws IOException {
        if (cancelled) throw new InterruptedIOException("Module download cancelled");
    }

    private static String strongEtag(String value) {
        if (value == null) return null;
        String tag = value.trim();
        return tag.length() >= 2 && tag.charAt(0) == '"' &&
                tag.charAt(tag.length() - 1) == '"' ? tag : null;
    }

    private static long validateRange(Reply reply, long start, long count, long expectedTotal)
            throws IOException {
        if (reply.status != 206 ||
                (reply.contentLength >= 0 && reply.contentLength != count) ||
                (reply.contentEncoding != null && !reply.contentEncoding.equalsIgnoreCase("identity"))) {
            throw new IOException("Server did not return the requested byte range");
        }
        Matcher matcher = CONTENT_RANGE.matcher(reply.contentRange == null ? "" : reply.contentRange.trim());
        if (!matcher.matches()) throw new IOException("Invalid Content-Range");
        try {
            long first = Long.parseLong(matcher.group(1));
            long last = Long.parseLong(matcher.group(2));
            long total = Long.parseLong(matcher.group(3));
            if (first != start || last != start + count - 1 || total <= last ||
                    (expectedTotal >= 0 && total != expectedTotal)) {
                throw new IOException("Incorrect Content-Range or module size changed");
            }
            return total;
        } catch (NumberFormatException e) {
            throw new IOException("Content-Range value is too large", e);
        }
    }
}
