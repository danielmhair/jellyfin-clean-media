using System.Text.Json;
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
/// <summary>Library items the review page wants status for.</summary>
public class StatusRequest
{
    public List<string>? ItemIds { get; set; }
}

/// <summary>A decision, a retime, or both, for one finding.</summary>
public class SegmentPatchRequest
{
    public bool? Approved { get; set; }

    public long? StartMs { get; set; }

    public long? EndMs { get; set; }

    public string? RecommendedAction { get; set; }

    /// <summary>Which fields the page actually meant to change.</summary>
    /// <remarks>
    /// Needed because null is meaningful for Approved — it clears a
    /// decision — and so cannot double as "not sent".
    /// </remarks>
    public List<string>? Fields { get; set; }
}

/// <summary>A finding added by hand.</summary>
public class SegmentCreateRequest
{
    public long StartMs { get; set; }

    public long EndMs { get; set; }

    public string Category { get; set; } = "manual";

    public string RecommendedAction { get; set; } = "skip";

    public string? Reasoning { get; set; }
}

/// <summary>Films to queue for analysis.</summary>
public class AnalyzeRequest
{
    public List<string>? ItemIds { get; set; }

    public string Engine { get; set; } = "subtitles";
}

/// <summary>A film to render a clean copy for.</summary>
public class RenderRequest
{
    public string? ItemId { get; set; }
}

/// <summary>A new front-first order for the queued jobs, by id.</summary>
public class ReorderRequest
{
    public List<string>? Ids { get; set; }
}

/// <summary>Pause or resume the whole queue.</summary>
public class PauseRequest
{
    public bool Paused { get; set; }
}

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

    /// <summary>Review state for a page of library items.</summary>
    /// <remarks>
    /// The review page lists items from Jellyfin's own API in the browser,
    /// then posts their ids here. Resolving id to file path has to happen on
    /// the server anyway, and proxying keeps the worker off the browser's
    /// network — it only ever needs to be reachable from Jellyfin.
    /// </remarks>
    [HttpPost("Status")]
    public async Task<ActionResult<object>> Status(
        [FromBody] StatusRequest request,
        CancellationToken cancellationToken)
    {
        var library = Plugin.LibraryManager;
        if (library is null)
        {
            return Ok(Array.Empty<object>());
        }

        // Keep ids and paths aligned: the worker answers positionally.
        var ids = new List<Guid>();
        var paths = new List<string>();
        foreach (var rawId in request.ItemIds ?? new List<string>())
        {
            if (!Guid.TryParse(rawId, out var id))
            {
                continue;
            }

            var path = library.GetItemById(id)?.Path;
            if (string.IsNullOrEmpty(path))
            {
                continue;
            }

            ids.Add(id);
            paths.Add(path);
        }

        if (paths.Count == 0)
        {
            return Ok(Array.Empty<object>());
        }

        var statuses = await _worker.GetStatusAsync(paths, cancellationToken).ConfigureAwait(false);
        if (statuses is null)
        {
            return Ok(new { unreachable = true });
        }

        return Ok(new
        {
            // Zip, not index: a short response must not throw.
            items = statuses.Zip(ids, (s, id) => new
            {
                itemId = id.ToString("N"),
                analyzed = s.Analyzed,
                total = s.Total,
                approved = s.Approved,
                rejected = s.Rejected,
                pending = s.Pending,
                job = s.Job,
                jobs = s.Jobs,
                enginesDone = s.EnginesDone,
            }),
        });
    }

    /// <summary>Resolve a library item id to the file path the worker needs.</summary>
    private static string? PathFor(string? itemId)
    {
        if (!Guid.TryParse(itemId, out var id))
        {
            return null;
        }

        var path = Plugin.LibraryManager?.GetItemById(id)?.Path;
        return string.IsNullOrEmpty(path) ? null : path;
    }

    /// <summary>Every finding for one film, reviewed or not.</summary>
    [HttpGet("Findings")]
    public async Task<ActionResult<object>> Findings(
        [FromQuery] string itemId,
        CancellationToken cancellationToken)
    {
        var path = PathFor(itemId);
        if (path is null)
        {
            return NotFound();
        }

        var result = await _worker.GetFindingsAsync(path, cancellationToken).ConfigureAwait(false);
        return Ok(new
        {
            unreachable = result.Unreachable,
            analyzed = result.Timeline is not null,
            segments = result.Timeline?.Segments,
        });
    }

    /// <summary>Approve, reject or retime a finding.</summary>
    [HttpPatch("Segments/{segmentId:int}")]
    public async Task<ActionResult<object>> PatchSegment(
        int segmentId,
        [FromQuery] string itemId,
        [FromBody] SegmentPatchRequest request,
        CancellationToken cancellationToken)
    {
        var path = PathFor(itemId);
        if (path is null)
        {
            return NotFound();
        }

        // Forward only what the page said it changed, so an omitted field
        // is never mistaken for "set this to null".
        var fields = request.Fields ?? new List<string>();
        var patch = new Dictionary<string, object?>();
        if (fields.Contains("approved"))
        {
            patch["approved"] = request.Approved;
        }

        if (fields.Contains("startMs") && request.StartMs is not null)
        {
            patch["startMs"] = request.StartMs;
        }

        if (fields.Contains("endMs") && request.EndMs is not null)
        {
            patch["endMs"] = request.EndMs;
        }

        if (fields.Contains("recommendedAction") && request.RecommendedAction is not null)
        {
            patch["recommendedAction"] = request.RecommendedAction;
        }

        if (patch.Count == 0)
        {
            return BadRequest(new { message = "nothing to change" });
        }

        var timeline = await _worker
            .PatchSegmentAsync(path, segmentId, patch, cancellationToken)
            .ConfigureAwait(false);
        return Ok(new { segments = timeline?.Segments });
    }

    /// <summary>Add a finding the engines missed.</summary>
    [HttpPost("Segments")]
    public async Task<ActionResult<object>> CreateSegment(
        [FromQuery] string itemId,
        [FromBody] SegmentCreateRequest request,
        CancellationToken cancellationToken)
    {
        var path = PathFor(itemId);
        if (path is null)
        {
            return NotFound();
        }

        var created = await _worker.CreateSegmentAsync(
            path,
            new
            {
                startMs = request.StartMs,
                endMs = request.EndMs,
                category = request.Category,
                recommendedAction = request.RecommendedAction,
                reasoning = request.Reasoning,
            },
            cancellationToken).ConfigureAwait(false);
        return Ok(created);
    }

    /// <summary>Delete a finding outright.</summary>
    [HttpDelete("Segments/{segmentId:int}")]
    public async Task<ActionResult<object>> DeleteSegment(
        int segmentId,
        [FromQuery] string itemId,
        CancellationToken cancellationToken)
    {
        var path = PathFor(itemId);
        if (path is null)
        {
            return NotFound();
        }

        var timeline = await _worker
            .DeleteSegmentAsync(path, segmentId, cancellationToken)
            .ConfigureAwait(false);
        return Ok(new { segments = timeline?.Segments });
    }

    /// <summary>A transcoded clip around a finding, when the browser cannot play the film itself.</summary>
    [HttpGet("Clip")]
    public async Task<ActionResult> Clip(
        [FromQuery] string itemId,
        [FromQuery] long startMs,
        [FromQuery] long endMs,
        CancellationToken cancellationToken)
    {
        var path = PathFor(itemId);
        if (path is null)
        {
            return NotFound();
        }

        var clip = await _worker.GetClipAsync(path, startMs, endMs, cancellationToken)
            .ConfigureAwait(false);
        if (clip is null)
        {
            return NotFound();
        }

        return File(clip.Value.Content, clip.Value.ContentType);
    }

    /// <summary>Queue films for analysis.</summary>
    [HttpPost("Analyze")]
    public async Task<ActionResult<object>> Analyze(
        [FromBody] AnalyzeRequest request,
        CancellationToken cancellationToken)
    {
        var queued = new List<object>();
        foreach (var itemId in request.ItemIds ?? new List<string>())
        {
            var path = PathFor(itemId);
            if (path is null)
            {
                continue;
            }

            try
            {
                var job = await _worker
                    .SubmitJobAsync(path, request.Engine, cancellationToken)
                    .ConfigureAwait(false);
                queued.Add(new { itemId, jobId = job?.Id, status = job?.Status });
            }
            catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException)
            {
                // One film the worker cannot see must not fail the batch.
                queued.Add(new { itemId, jobId = (string?)null, error = ex.Message });
            }
        }

        return Ok(new { queued });
    }

    /// <summary>Render a clean copy of one film from its approved findings.</summary>
    /// <remarks>
    /// Mutes and blurs cannot be applied during playback, so acting on those
    /// findings means writing a separate clean copy. The worker reads the
    /// reviewed sidecar and acts only on approved findings; the original file
    /// is never modified. Returns a job id the page polls via RenderStatus.
    /// </remarks>
    [HttpPost("Render")]
    public async Task<ActionResult<object>> Render(
        [FromBody] RenderRequest request,
        CancellationToken cancellationToken)
    {
        var path = PathFor(request.ItemId);
        if (path is null)
        {
            return NotFound();
        }

        var result = await _worker.RenderAsync(path, cancellationToken).ConfigureAwait(false);
        if (result.Unreachable)
        {
            return Ok(new { unreachable = true });
        }

        if (result.Error is not null)
        {
            return Ok(new { error = result.Error });
        }

        return Ok(new { jobId = result.Job?.Id, status = result.Job?.Status });
    }

    /// <summary>Progress of a render job, for polling from the film view.</summary>
    [HttpGet("RenderStatus")]
    public async Task<ActionResult<object>> RenderStatus(
        [FromQuery] string jobId,
        CancellationToken cancellationToken)
    {
        var job = await _worker.GetJobAsync(jobId, cancellationToken).ConfigureAwait(false);
        if (job is null)
        {
            return Ok(new { unreachable = true });
        }

        return Ok(new
        {
            status = job.Status,
            progress = job.Progress,
            stage = job.Stage,
            error = job.Error,
            renderedPath = job.RenderedPath,
        });
    }

    /// <summary>Jobs the worker knows about, for progress and cancellation.</summary>
    [HttpGet("Jobs")]
    public async Task<ActionResult<object>> Jobs(CancellationToken cancellationToken)
    {
        var jobs = await _worker.ListJobsAsync(cancellationToken).ConfigureAwait(false);
        if (jobs is null)
        {
            return Ok(new { unreachable = true });
        }

        return Ok(new { jobs });
    }

    /// <summary>Cancel a queued or finished job.</summary>
    [HttpDelete("Jobs/{jobId}")]
    public async Task<ActionResult<object>> CancelJob(
        string jobId,
        CancellationToken cancellationToken)
    {
        var ok = await _worker.DeleteJobAsync(jobId, cancellationToken).ConfigureAwait(false);
        return Ok(new { ok });
    }

    /// <summary>Cancel every queued or running job at once.</summary>
    [HttpPost("Jobs/CancelAll")]
    public async Task<ActionResult<object>> CancelAllJobs(CancellationToken cancellationToken)
    {
        var cancelled = await _worker.CancelAllJobsAsync(cancellationToken).ConfigureAwait(false);
        if (cancelled is null)
        {
            return Ok(new { unreachable = true });
        }

        return Ok(new { cancelled });
    }

    /// <summary>Reorder the queued jobs, front-first, by id.</summary>
    [HttpPost("Jobs/Reorder")]
    public async Task<ActionResult<object>> ReorderJobs(
        [FromBody] ReorderRequest request,
        CancellationToken cancellationToken)
    {
        var jobs = await _worker
            .ReorderJobsAsync(request.Ids ?? new List<string>(), cancellationToken)
            .ConfigureAwait(false);
        if (jobs is null)
        {
            return Ok(new { unreachable = true });
        }

        return Ok(new { jobs });
    }

    /// <summary>Retry a failed or cancelled job from the back of the queue.</summary>
    [HttpPost("Jobs/{jobId}/Requeue")]
    public async Task<ActionResult<object>> RequeueJob(
        string jobId,
        CancellationToken cancellationToken)
    {
        var (status, job) = await _worker.RequeueJobAsync(jobId, cancellationToken).ConfigureAwait(false);
        if (status == 0)
        {
            return Ok(new { unreachable = true });
        }

        if (status == 404)
        {
            return Ok(new { error = "That job no longer exists." });
        }

        if (status == 400)
        {
            return Ok(new { error = "Only a failed or cancelled job can be requeued." });
        }

        return Ok(new { jobId = job?.Id, status = job?.Status });
    }

    /// <summary>Pause or resume the whole queue (never preempts a running job).</summary>
    [HttpPost("Jobs/Pause")]
    public async Task<ActionResult<object>> PauseQueue(
        [FromBody] PauseRequest request,
        CancellationToken cancellationToken)
    {
        var paused = await _worker.SetPausedAsync(request.Paused, cancellationToken).ConfigureAwait(false);
        if (paused is null)
        {
            return Ok(new { unreachable = true });
        }

        return Ok(new { paused });
    }

    /// <summary>The worker's analysis schedule, for the settings page to edit.</summary>
    /// <remarks>
    /// The schedule lives on the worker (the machine that enforces it), so the
    /// settings page reads and writes it through here rather than storing it in
    /// the Jellyfin plugin config — one source of truth, no drift. The shape is
    /// the worker's, passed through opaquely.
    /// </remarks>
    [HttpGet("Schedule")]
    public async Task<ActionResult<object>> GetSchedule(CancellationToken cancellationToken)
    {
        var view = await _worker.GetScheduleAsync(cancellationToken).ConfigureAwait(false);
        if (view is null)
        {
            return Ok(new { unreachable = true });
        }

        return Ok(view);
    }

    /// <summary>Replace the worker's analysis schedule.</summary>
    [HttpPost("Schedule")]
    public async Task<ActionResult<object>> SetSchedule(
        [FromBody] JsonElement schedule,
        CancellationToken cancellationToken)
    {
        var view = await _worker.SetScheduleAsync(schedule, cancellationToken).ConfigureAwait(false);
        if (view is null)
        {
            return Ok(new { unreachable = true });
        }

        return Ok(view);
    }

    /// <summary>Bits of plugin config the review page needs in the browser.</summary>
    /// <remarks>
    /// The worker URL lets the page open the worker's own standalone review
    /// page for a film. It is only exposed to an elevated admin (this whole
    /// controller requires elevation), and it is the same URL the admin typed
    /// into settings.
    /// </remarks>
    [HttpGet("Config")]
    public ActionResult<object> Config()
    {
        var workerUrl = (Plugin.Instance?.Configuration.WorkerUrl ?? string.Empty).TrimEnd('/');
        return Ok(new { workerUrl });
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
            paused = health.Paused,
            gpu = health.Gpu is null || !health.Gpu.Available ? "none" : health.Gpu.Name,
        });
    }
}
