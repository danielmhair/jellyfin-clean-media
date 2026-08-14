using MediaBrowser.Controller;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.CleanMedia;

/// <summary>
/// Adds a &lt;script&gt; tag to the web client's index.html so the "flag this
/// moment" button appears in the player.
///
/// Jellyfin has no server-side API for adding player UI, so the only way to put
/// a button on the control bar is to load a script into the web client. We do
/// that by patching index.html on startup. The patch is idempotent and re-runs
/// every launch, so a Jellyfin upgrade that rewrites index.html is repaired the
/// next time the server starts.
///
/// If the web directory is read-only (some Docker images), patching fails
/// harmlessly: we log how to add the one-line tag by hand, and everything else
/// (the served script, the endpoints) still works.
/// </summary>
public class WebInjectionService : IHostedService
{
    // Relative to /web/index.html, so it resolves under any base-path prefix.
    private const string Marker = "<script defer src=\"../CleanMedia/PlayerScript.js\"></script>";

    private readonly IServerApplicationPaths _paths;
    private readonly ILogger<WebInjectionService> _logger;

    public WebInjectionService(IServerApplicationPaths paths, ILogger<WebInjectionService> logger)
    {
        _paths = paths;
        _logger = logger;
    }

    /// <inheritdoc />
    public Task StartAsync(CancellationToken cancellationToken)
    {
        try
        {
            var webPath = _paths.WebPath;
            var indexPath = Path.Combine(webPath, "index.html");
            if (!File.Exists(indexPath))
            {
                _logger.LogWarning(
                    "Clean Media: index.html not found at {Path}; player flag button not installed.",
                    indexPath);
                return Task.CompletedTask;
            }

            var html = File.ReadAllText(indexPath);
            if (html.Contains(Marker, StringComparison.Ordinal))
            {
                return Task.CompletedTask;
            }

            var closeBody = html.LastIndexOf("</body>", StringComparison.OrdinalIgnoreCase);
            if (closeBody < 0)
            {
                _logger.LogWarning(
                    "Clean Media: no </body> in index.html; add {Marker} by hand to enable the player flag button.",
                    Marker);
                return Task.CompletedTask;
            }

            var patched = html.Insert(closeBody, Marker + "\n");
            File.WriteAllText(indexPath, patched);
            _logger.LogInformation("Clean Media: installed the player flag button into {Path}.", indexPath);
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            _logger.LogWarning(
                ex,
                "Clean Media: could not patch index.html (is the web directory read-only?). "
                + "Add {Marker} before </body> by hand to enable the player flag button.",
                Marker);
        }

        return Task.CompletedTask;
    }

    /// <inheritdoc />
    public Task StopAsync(CancellationToken cancellationToken) => Task.CompletedTask;
}
