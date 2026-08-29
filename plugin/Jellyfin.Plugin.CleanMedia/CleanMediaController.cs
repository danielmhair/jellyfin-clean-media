using System.Text.Json;
using Jellyfin.Data.Enums;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Library;
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

    /// <summary>Optional per-item version paths, aligned with ItemIds.</summary>
    /// <remarks>
    /// The film grid leaves this empty and gets each item's own file. The
    /// per-film dialog sends the version the administrator picked, so the
    /// status it shows (findings, approvals, clean copy) is that version's.
    /// </remarks>
    public List<string>? Paths { get; set; }
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

    /// <summary>Which version of the film to work on, by file path.</summary>
    /// <remarks>
    /// A rendered clean copy is an alternate *version* of the movie, not a
    /// library item of its own, so an item id alone can only ever mean the
    /// original. Sent when the administrator picked a version; ignored unless
    /// it is genuinely one of that item's versions.
    /// </remarks>
    public string? Path { get; set; }
}

/// <summary>A film to render a clean copy for.</summary>
public class RenderRequest
{
    public string? ItemId { get; set; }

    /// <summary>Which version of the film to render from. See AnalyzeRequest.Path.</summary>
    public string? Path { get; set; }

    /// <summary>"replace" the clean copy this supersedes, or write a "new" one beside it.</summary>
    public string Mode { get; set; } = "replace";
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
    /// <summary>
    /// The worker HTTP-contract version this plugin build was written against.
    /// It's compared with the worker's reported apiVersion so a mismatched pair
    /// is reported plainly on the settings page instead of failing silently.
    /// Bump this (and the worker's API_VERSION) together, only on a breaking
    /// change to the /api surface.
    /// </summary>
    public const int SupportedWorkerApiVersion = 1;

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
        var rawIds = request.ItemIds ?? new List<string>();
        for (var i = 0; i < rawIds.Count; i++)
        {
            var rawId = rawIds[i];
            if (!Guid.TryParse(rawId, out var id))
            {
                continue;
            }

            // The grid sends ids only and gets each movie's own file; the
            // per-film dialog also sends the version it is showing, so the
            // counts it displays belong to that version.
            var wanted = request.Paths is not null && request.Paths.Count > i
                ? request.Paths[i]
                : null;
            var path = PathFor(rawId, wanted);
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
                cleanCopy = s.CleanCopy,
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

    /// <summary>The file path for an item, honouring a version the caller named.</summary>
    /// <remarks>
    /// A rendered clean copy is an alternate *version* of the movie, not a
    /// library item of its own, so an item id alone can only ever mean the
    /// original — which is why the queue page could not offer the clean copy.
    /// A named path is accepted only when it really is one of that item's
    /// versions, so the page can pick one without being able to point the
    /// worker at an arbitrary file; anything else falls back to the original.
    /// </remarks>
    private static string? PathFor(string? itemId, string? preferred)
    {
        var own = PathFor(itemId);
        if (string.IsNullOrEmpty(preferred) || !Guid.TryParse(itemId, out var id))
        {
            return own;
        }

        var match = VersionPathsFor(id)
            .FirstOrDefault(v => string.Equals(v, preferred, StringComparison.OrdinalIgnoreCase));
        return match ?? own;
    }

    /// <summary>Every file Jellyfin holds as a version of one item, original first.</summary>
    private static List<string> VersionPathsFor(Guid id)
    {
        var paths = new List<string>();
        var item = Plugin.LibraryManager?.GetItemById(id);
        if (item is null)
        {
            return paths;
        }

        if (!string.IsNullOrEmpty(item.Path))
        {
            paths.Add(item.Path);
        }

        if (item is Video video)
        {
            // Same-folder files named "<folder> - <label>" — where a rendered
            // clean copy lands — plus versions linked in by hand.
            foreach (var local in video.LocalAlternateVersions ?? Array.Empty<string>())
            {
                if (!string.IsNullOrEmpty(local))
                {
                    paths.Add(local);
                }
            }

            foreach (var linked in video.GetLinkedAlternateVersions())
            {
                if (!string.IsNullOrEmpty(linked.Path))
                {
                    paths.Add(linked.Path);
                }
            }
        }

        return paths.Distinct(StringComparer.OrdinalIgnoreCase).ToList();
    }

    /// <summary>The label Jellyfin lists a version file under.</summary>
    /// <remarks>
    /// "Movie (2014)/Movie (2014) - Clean.mkv" is the version "Clean"; the
    /// folder-named file is the original. Mirrors the worker's naming rule
    /// (worker/cleancopy.py) so both sides call the same file the same thing.
    /// </remarks>
    private static string VersionLabel(string path)
    {
        var stem = System.IO.Path.GetFileNameWithoutExtension(path) ?? string.Empty;
        var folder = System.IO.Path.GetFileName(
            System.IO.Path.GetDirectoryName(path) ?? string.Empty) ?? string.Empty;
        if (folder.Length == 0)
        {
            return stem;
        }

        if (string.Equals(stem, folder, StringComparison.OrdinalIgnoreCase))
        {
            return "Original";
        }

        var prefix = folder + " - ";
        return stem.StartsWith(prefix, StringComparison.OrdinalIgnoreCase)
            ? stem[prefix.Length..]
            : stem;
    }

    /// <summary>The versions of one film the page can pick between.</summary>
    /// <remarks>
    /// The film list comes from Jellyfin's own API, which returns one item per
    /// movie — a rendered clean copy is folded into it as an alternate version
    /// and never appears on its own. This is how the page gets at it, so a pass
    /// (or a re-render) can be aimed at the clean copy the administrator is
    /// actually watching.
    /// </remarks>
    [HttpGet("Versions")]
    public ActionResult<object> Versions([FromQuery] string itemId)
    {
        if (!Guid.TryParse(itemId, out var id))
        {
            return NotFound();
        }

        var paths = VersionPathsFor(id);
        if (paths.Count == 0)
        {
            return NotFound();
        }

        return Ok(new
        {
            versions = paths.Select((p, i) => new
            {
                path = p,
                name = VersionLabel(p),
                isPrimary = i == 0,
            }),
        });
    }

    /// <summary>Where a render of one version would write, and what it replaces.</summary>
    /// <remarks>
    /// Asked before queueing a render so the administrator is not surprised: a
    /// clean copy is a file they may be part-way through watching, and the
    /// choice is theirs — overwrite it, or keep it and make another.
    /// </remarks>
    [HttpGet("RenderPlan")]
    public async Task<ActionResult<object>> RenderPlan(
        [FromQuery] string itemId,
        [FromQuery] string? path,
        CancellationToken cancellationToken)
    {
        var resolved = PathFor(itemId, path);
        if (resolved is null)
        {
            return NotFound();
        }

        var plan = await _worker.GetRenderPlanAsync(resolved, cancellationToken).ConfigureAwait(false);
        if (plan is null)
        {
            return Ok(new { unreachable = true });
        }

        return Ok(new
        {
            sourceIsCleanCopy = plan.SourceIsCleanCopy,
            replacePath = plan.ReplacePath,
            replaceLabel = plan.ReplaceLabel,
            replaceExists = plan.ReplaceExists,
            newPath = plan.NewPath,
            newLabel = plan.NewLabel,
        });
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
            var path = PathFor(itemId, request.Path);
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
        var path = PathFor(request.ItemId, request.Path);
        if (path is null)
        {
            return NotFound();
        }

        var mode = string.Equals(request.Mode, "new", StringComparison.OrdinalIgnoreCase)
            ? "new"
            : "replace";
        var result = await _worker.RenderAsync(path, mode, cancellationToken).ConfigureAwait(false);
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

    /// <summary>Worker-owned settings — media roots, VLM guidance/hosts, and the
    /// policy/profanity toggles — for the Settings/Advanced tabs to edit.</summary>
    /// <remarks>Same reasoning as Schedule: these live on the worker (the
    /// machine that actually resolves paths and calls the VLM), so the
    /// settings page reads and writes them through here.</remarks>
    [HttpGet("Settings")]
    public async Task<ActionResult<object>> GetSettings(CancellationToken cancellationToken)
    {
        var view = await _worker.GetWorkerSettingsAsync(cancellationToken).ConfigureAwait(false);
        if (view is null)
        {
            return Ok(new { unreachable = true });
        }

        return Ok(view);
    }

    /// <summary>Replace the worker's settings.</summary>
    [HttpPost("Settings")]
    public async Task<ActionResult<object>> SetSettings(
        [FromBody] JsonElement settings,
        CancellationToken cancellationToken)
    {
        var view = await _worker.SetWorkerSettingsAsync(settings, cancellationToken).ConfigureAwait(false);
        if (view is null)
        {
            return Ok(new { unreachable = true });
        }

        return Ok(view);
    }

    /// <summary>List subdirectories on the worker's own filesystem, for the media-roots folder picker.</summary>
    /// <remarks>The plugin's browser is often on a different machine from the
    /// worker (and its filesystem), so a native OS file dialog can't work
    /// here — this browses the worker's filesystem over the API instead, the
    /// same approach Sonarr/Radarr/Plex use for library folders.</remarks>
    [HttpGet("Browse")]
    public async Task<ActionResult<object>> Browse(
        [FromQuery] string path,
        CancellationToken cancellationToken)
    {
        var result = await _worker.BrowseAsync(path ?? string.Empty, cancellationToken).ConfigureAwait(false);
        if (result is null)
        {
            return Ok(new { unreachable = true });
        }

        return Ok(result);
    }

    /// <summary>Whether the worker is up and whether the recovery helper is
    /// currently allowed to act. Null/unreachable when even the helper can't
    /// be reached (an older install that hasn't re-run install-service since
    /// this shipped, or the helper genuinely being down too).</summary>
    [HttpGet("Supervisor")]
    public async Task<ActionResult<object>> GetSupervisorStatus(CancellationToken cancellationToken)
    {
        var status = await _worker.GetSupervisorStatusAsync(cancellationToken).ConfigureAwait(false);
        if (status is null)
        {
            return Ok(new { unreachable = true });
        }

        return Ok(status);
    }

    /// <summary>Start the worker via the recovery helper — works even when the worker is completely down.</summary>
    [HttpPost("Supervisor/Start")]
    public async Task<ActionResult<object>> SupervisorStart(CancellationToken cancellationToken)
    {
        var result = await _worker.SupervisorStartAsync(cancellationToken).ConfigureAwait(false);
        return result is null ? Ok(new { unreachable = true }) : Ok(result);
    }

    /// <summary>Restart the worker via the recovery helper. Safe to use anytime
    /// — any job in progress resumes automatically (the queue re-queues on
    /// startup, and the visual pass resumes from its own checkpoint).</summary>
    [HttpPost("Supervisor/Restart")]
    public async Task<ActionResult<object>> SupervisorRestart(CancellationToken cancellationToken)
    {
        var result = await _worker.SupervisorRestartAsync(cancellationToken).ConfigureAwait(false);
        return result is null ? Ok(new { unreachable = true }) : Ok(result);
    }

    /// <summary>Stop the worker via the recovery helper.</summary>
    [HttpPost("Supervisor/Stop")]
    public async Task<ActionResult<object>> SupervisorStop(CancellationToken cancellationToken)
    {
        var result = await _worker.SupervisorStopAsync(cancellationToken).ConfigureAwait(false);
        return result is null ? Ok(new { unreachable = true }) : Ok(result);
    }

    /// <summary>Allow the recovery helper to start/restart the worker again after it was disabled.</summary>
    [HttpPost("Supervisor/Enable")]
    public async Task<ActionResult<object>> SupervisorEnable(CancellationToken cancellationToken)
    {
        var result = await _worker.SupervisorEnableAsync(cancellationToken).ConfigureAwait(false);
        return result is null ? Ok(new { unreachable = true }) : Ok(result);
    }

    /// <summary>Stop the recovery helper from starting/restarting the worker —
    /// it keeps listening (so this can always be undone from here later),
    /// it just stops acting.</summary>
    [HttpPost("Supervisor/Disable")]
    public async Task<ActionResult<object>> SupervisorDisable(CancellationToken cancellationToken)
    {
        var result = await _worker.SupervisorDisableAsync(cancellationToken).ConfigureAwait(false);
        return result is null ? Ok(new { unreachable = true }) : Ok(result);
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

    /// <summary>The player flag script, injected into the web client.</summary>
    /// <remarks>
    /// Anonymous because the web client loads it for every user; it is inert
    /// static text and only reveals the button to administrators. Creating a
    /// finding still requires elevation on <see cref="CreateSegment"/>.
    /// </remarks>
    [AllowAnonymous]
    [HttpGet("PlayerScript.js")]
    [Produces("application/javascript")]
    public ActionResult PlayerScript()
    {
        var asm = typeof(CleanMediaController).Assembly;
        var stream = asm.GetManifestResourceStream(
            "Jellyfin.Plugin.CleanMedia.Web.PlayerFlag.js");
        if (stream is null)
        {
            return NotFound();
        }

        return File(stream, "application/javascript");
    }

    /// <summary>Settings the player script reads before showing its button.</summary>
    [AllowAnonymous]
    [HttpGet("PlayerConfig")]
    public ActionResult<object> PlayerConfig()
    {
        var config = Plugin.Instance?.Configuration;
        return Ok(new
        {
            enabled = config?.FlagButtonEnabled ?? true,
            padMs = config?.FlagPadMs ?? 1500,
        });
    }

    /// <summary>The worker's review page URL for one film, for the player button.</summary>
    /// <remarks>
    /// Resolving item id to path and reading the worker URL both have to happen
    /// on the server, so the player's "review this film" button asks here rather
    /// than assembling the URL itself. Elevation-required, like every write on
    /// this controller — the button is only shown to administrators anyway.
    /// </remarks>
    [HttpGet("ReviewUrl")]
    public ActionResult<object> ReviewUrl(
        [FromQuery] string itemId,
        [FromQuery] string? version = null)
    {
        var path = PathFor(itemId, version);
        if (path is null)
        {
            return NotFound();
        }

        var workerUrl = (Plugin.Instance?.Configuration.WorkerUrl ?? string.Empty).TrimEnd('/');
        if (workerUrl.Length == 0)
        {
            return Ok(new { url = (string?)null });
        }

        var url = workerUrl + "/api/review?path=" + Uri.EscapeDataString(path);
        return Ok(new { url });
    }

    /// <summary>Resolve a file path back to a Jellyfin library item id, so the
    /// review page can open a film straight in Jellyfin.</summary>
    /// <remarks>
    /// A render writes a separate clean copy; if that file is in the library
    /// (its own item) we return it, otherwise we fall back to the original
    /// film — which is where approved skips already apply during playback.
    /// </remarks>
    [HttpGet("ItemForPath")]
    public ActionResult<object> ItemForPath(
        [FromQuery] string path,
        [FromQuery] string? fallback = null)
    {
        var library = Plugin.LibraryManager;
        if (library is null)
        {
            return NotFound();
        }

        // 1) Exact path match — works when Jellyfin and the worker share a mount.
        var clean = string.IsNullOrEmpty(path) ? null : library.FindByPath(path, false);
        var item = clean
            ?? (string.IsNullOrEmpty(fallback) ? null : library.FindByPath(fallback, false));

        // 2) Foreign mounts: the worker's path (e.g. a \\NAS UNC) need not equal
        //    Jellyfin's path for the same file. Fall back to matching the
        //    original film by file name, which is mount-independent. The clean
        //    copy is a *version* of that item, so we match the original's name.
        if (item is null)
        {
            var fileName = System.IO.Path.GetFileName(
                string.IsNullOrEmpty(fallback) ? (path ?? string.Empty) : fallback);
            if (!string.IsNullOrEmpty(fileName))
            {
                item = FindByFileName(library, fileName);
            }
        }

        if (item is null)
        {
            return NotFound();
        }

        return Ok(new { itemId = item.Id.ToString("N"), isCleanCopy = clean is not null });
    }

    /// <summary>Find a movie/episode whose file name matches, ignoring the
    /// directory — so a worker path on a different mount still resolves.</summary>
    private static BaseItem? FindByFileName(ILibraryManager library, string fileName)
    {
        var query = new InternalItemsQuery
        {
            IncludeItemTypes = new[] { BaseItemKind.Movie, BaseItemKind.Episode },
            Recursive = true,
        };
        foreach (var candidate in library.GetItemList(query))
        {
            var candidatePath = candidate.Path;
            if (!string.IsNullOrEmpty(candidatePath)
                && string.Equals(
                    System.IO.Path.GetFileName(candidatePath),
                    fileName,
                    System.StringComparison.OrdinalIgnoreCase))
            {
                return candidate;
            }
        }

        return null;
    }

    /// <summary>Ask Jellyfin to scan its libraries now.</summary>
    /// <remarks>
    /// A freshly rendered clean copy is a new file in the movie's folder; until
    /// Jellyfin scans it, it isn't attached to the movie as a selectable
    /// version. The review page calls this once when a render finishes. The scan
    /// is incremental — it only picks up new/changed files — so it is cheap.
    /// </remarks>
    [HttpPost("Rescan")]
    public ActionResult<object> Rescan()
    {
        var library = Plugin.LibraryManager;
        if (library is null)
        {
            return Ok(new { ok = false });
        }

        library.QueueLibraryScan();
        return Ok(new { ok = true });
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

        var (compatible, compatMessage) = CheckApiCompatibility(health.ApiVersion);

        return Ok(new
        {
            ok = true,
            version = health.Version,
            apiVersion = health.ApiVersion,
            pluginApiVersion = SupportedWorkerApiVersion,
            compatible,
            compatMessage,
            engines = health.Engines.Keys,
            queueSize = health.QueueSize,
            paused = health.Paused,
            gpu = health.Gpu is null || !health.Gpu.Available ? "none" : health.Gpu.Name,
            updateAvailable = health.UpdateAvailable,
            latestVersion = health.LatestVersion,
        });
    }

    /// <summary>
    /// Start applying the latest release on the worker. Never called except by
    /// an explicit "Update now" click on the settings page — see worker/update.py.
    /// </summary>
    [HttpPost("Update")]
    public async Task<ActionResult<object>> ApplyUpdate(CancellationToken cancellationToken)
    {
        var (ok, error) = await _worker.ApplyUpdateAsync(cancellationToken).ConfigureAwait(false);
        return Ok(new { ok, error });
    }

    /// <summary>Progress of an in-progress (or just-finished) update, for the settings page to poll.</summary>
    [HttpGet("UpdateStatus")]
    public async Task<ActionResult<object>> UpdateStatus(CancellationToken cancellationToken)
    {
        var status = await _worker.GetUpdateStatusAsync(cancellationToken).ConfigureAwait(false);
        if (status is null)
        {
            return Ok(new { unreachable = true });
        }

        return Ok(status);
    }

    /// <summary>
    /// Compare the worker's contract version with the one this plugin expects.
    /// Returns whether they can talk, and if not, a message that names which
    /// side is behind and the one action to fix it.
    /// </summary>
    private static (bool Compatible, string? Message) CheckApiCompatibility(int workerApiVersion)
    {
        if (workerApiVersion == 0)
        {
            // Predates the handshake — too old to advertise a contract version.
            return (false,
                "This worker is older than the plugin and doesn't report an API version. "
                + "Update the worker: git pull, then run scripts/install-service.ps1 -Restart.");
        }

        if (workerApiVersion < SupportedWorkerApiVersion)
        {
            return (false,
                $"The worker speaks API v{workerApiVersion} but this plugin needs "
                + $"v{SupportedWorkerApiVersion}. Update the worker to match this plugin: "
                + "git pull, then run scripts/install-service.ps1 -Restart.");
        }

        if (workerApiVersion > SupportedWorkerApiVersion)
        {
            return (false,
                $"The worker speaks API v{workerApiVersion} but this plugin only understands "
                + $"v{SupportedWorkerApiVersion}. Update the plugin to match the worker "
                + "(Dashboard → Plugins → Clean Media → update to the version matching this worker).");
        }

        return (true, null);
    }
}
