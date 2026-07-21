using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Mvc;

namespace Jellyfin.Plugin.CleanMedia;

/// <summary>
/// Endpoints for the settings page.
///
/// The connection test runs here, on the server, rather than as a fetch()
/// from the browser. The browser is on a different origin from the worker,
/// so a direct call is blocked by CORS ("TypeError: Failed to fetch") — and
/// it would be testing the wrong machine anyway. What matters is whether
/// the Jellyfin server can reach the worker, because that is what fetches
/// segments during playback.
/// </summary>
[ApiController]
[Authorize(Policy = "RequiresElevation")]
[Route("CleanMedia")]
[Produces("application/json")]
public class CleanMediaController : ControllerBase
{
    private readonly WorkerClient _worker;

    public CleanMediaController(WorkerClient worker)
    {
        _worker = worker;
    }

    /// <summary>Ask the worker for its health, from the server.</summary>
    [HttpGet("TestConnection")]
    public async Task<ActionResult<object>> TestConnection(CancellationToken cancellationToken)
    {
        var health = await _worker.GetHealthAsync(cancellationToken).ConfigureAwait(false);
        if (health is null)
        {
            return Ok(new
            {
                ok = false,
                message = "Jellyfin could not reach the worker at "
                          + (Plugin.Instance?.Configuration.WorkerUrl ?? "(unset)")
                          + ". Check the URL, that the worker is running, and that "
                          + "its port is open to this server.",
            });
        }

        return Ok(new
        {
            ok = true,
            version = health.Version,
            engines = health.Engines.Keys,
            queueSize = health.QueueSize,
            gpu = health.Gpu is null || !health.Gpu.Available ? "none" : health.Gpu.Name,
        });
    }
}
