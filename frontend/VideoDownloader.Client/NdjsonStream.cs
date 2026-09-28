using System.Buffers;
using System.Text;

namespace VideoDownloader.Client;

/// <summary>
/// One JSON object per line, UTF-8, newline-terminated.
/// </summary>
/// <remarks>
/// Hand-rolled rather than a <see cref="StreamReader"/> for one reason: the size
/// cap. The core closes a connection that sends a line past 8 MiB, and a front
/// end that would happily buffer an unbounded line before noticing is the same
/// defect pointing the other way. A process that starts writing rubbish must not
/// be able to exhaust memory here.
/// </remarks>
internal sealed class NdjsonStream : IDisposable
{
    /// <summary>Same cap as the core's MAX_LINE_BYTES.</summary>
    public const int MaxLineBytes = 8 * 1024 * 1024;

    private static readonly UTF8Encoding Utf8 = new(encoderShouldEmitUTF8Identifier: false, throwOnInvalidBytes: false);

    private readonly Stream _stream;
    private readonly byte[] _buffer = ArrayPool<byte>.Shared.Rent(16 * 1024);
    private readonly MemoryStream _pending = new();
    private int _start;
    private int _count;
    private bool _disposed;

    public NdjsonStream(Stream stream) => _stream = stream;

    /// <summary>The next line, or null once the peer closed the connection.</summary>
    public async Task<string?> ReadLineAsync(CancellationToken cancellationToken)
    {
        while (true)
        {
            var newline = Array.IndexOf(_buffer, (byte)'\n', _start, _count);
            if (newline >= 0)
            {
                var length = newline - _start;
                _pending.Write(_buffer, _start, length);
                _count -= length + 1;
                _start = newline + 1;

                var line = Utf8.GetString(_pending.GetBuffer(), 0, (int)_pending.Length);
                _pending.SetLength(0);
                return line.TrimEnd('\r');
            }

            // No newline in what we hold: keep it and read more.
            _pending.Write(_buffer, _start, _count);
            if (_pending.Length > MaxLineBytes)
            {
                throw new CoreProtocolException($"the core sent a line past {MaxLineBytes} bytes");
            }

            _start = 0;
            _count = await _stream.ReadAsync(_buffer.AsMemory(), cancellationToken).ConfigureAwait(false);
            if (_count == 0)
            {
                // A trailing line without its newline is still a line; anything
                // else at this point is a clean end of stream.
                if (_pending.Length == 0)
                {
                    return null;
                }

                var tail = Utf8.GetString(_pending.GetBuffer(), 0, (int)_pending.Length);
                _pending.SetLength(0);
                return tail.TrimEnd('\r');
            }
        }
    }

    public async Task WriteLineAsync(string line, CancellationToken cancellationToken)
    {
        var bytes = Utf8.GetBytes(line + "\n");
        await _stream.WriteAsync(bytes.AsMemory(), cancellationToken).ConfigureAwait(false);
        await _stream.FlushAsync(cancellationToken).ConfigureAwait(false);
    }

    public void Dispose()
    {
        if (_disposed)
        {
            return;
        }

        _disposed = true;
        ArrayPool<byte>.Shared.Return(_buffer);
        _pending.Dispose();
    }
}
