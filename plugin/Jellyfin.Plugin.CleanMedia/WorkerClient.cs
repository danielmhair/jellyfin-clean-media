using System.Net.Http.Json;
using System.Text.Json;
using System.Text.Json.Serialization;
using Jellyfin.Plugin.CleanMedia.Configuration;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.CleanMedia;

/// <summary>One segment in the worker's engine-agnostic timeline.</summary>
public class TimelineSegment
{
    [JsonPropertyName("id")] public int Id { get; set; }

    [JsonPropertyName("startMs")] public long StartMs { get; set; }

    [JsonPropertyName("endMs")] public long EndMs { get; set; }

    [JsonPropertyName("category")] public string Category { get; set; } = string.Empty;

    [JsonPropertyName("confidence")] public double Confidence { get; set; }

    [JsonPropertyName("engine")] public string Engine { get; set; } = string.Empty;

    [JsonPropertyName("recommendedAction")] public string RecommendedAction { get; set; } = string.Empty;

    [JsonPropertyName("approved")] public bool? Approved { get; set; }

    [JsonPropertyName("reasoning")] public string? Reasoning { get; set; }

    [JsonPropertyName("engineRef")] public string? EngineRef { get; set; }
}

/// <summary>An analysis job as the review page lists it.</summary>
public class WorkerJob
{
    [JsonPropertyName("id")] public string Id { get; set; } = string.Empty;

    [JsonPropertyName("mediaPath")] public string MediaPath { get; set; } = string.Empty;

    [JsonPropertyName("engine")] public string Engine { get; set; } = string.Empty;

    [JsonPropertyName("status")] public string Status { get; set; } = string.Empty;

    [JsonPropertyName("progress")] public double Progress { get; set; }

    [JsonPropertyName("stage")] public string Stage { get; set; } = string.Empty;

    [JsonPropertyName("error")] public string? Error { get; set; }

    /// <summary>Where a completed render wrote the clean copy, if any.</summary>
    [JsonPropertyName("renderedPath")] public string? RenderedPath { get; set; }
}

/// <summary>The worker's standard timeline response.</summary>
public class Timeline
{
    [JsonPropertyName("schemaVersion")] public int SchemaVersion { get; set; }

    [JsonPropertyName("mediaFingerprint")] public string MediaFingerprint { get; set; } = string.Empty;

    [JsonPropertyName("segments")] public List<TimelineSegment> Segments { get; set; } = new();
}

/// <summary>
/// Outcome of asking the worker for a film's findings. Separates an
/// unreachable worker from a film that simply has not been analyzed, so the
/// review page can say which is true rather than showing "no findings" for a
/// worker that is actually down.
/// </summary>
public class FindingsResult
{
    /// <summary>The worker could not be reached (down, wrong URL, or timed out).</summary>
    public bool Unreachable { get; init; }

    /// <summary>The film's findings, or null when the worker has not analyzed it.</summary>
    public Timeline? Timeline { get; init; }
}

/// <summary>GPU details from the worker's health endpoint.</summary>
public class GpuInfo
{
    [JsonPropertyName("available")] public bool Available { get; set; }

    [JsonPropertyName("name")] public string? Name { get; set; }
}

/// <summary>The worker's health response.</summary>
public class WorkerHealth
{
    [JsonPropertyName("status")] public string Status { get; set; } = string.Empty;

    [JsonPropertyName("version")] public string Version { get; set; } = string.Empty;

    [JsonPropertyName("queueSize")] public int QueueSize { get; set; }

    [JsonPropertyName("gpu")] public GpuInfo? Gpu { get; set; }

    [JsonPropertyName("engines")]
    public Dictionary<string, object> Engines { get; set; } = new();
}

/// <summary>Analysis job progress, as the review page shows it.</summary>
public class JobBrief
{
    [JsonPropertyName("id")] public string Id { get; set; } = string.Empty;

    [JsonPropertyName("status")] public string Status { get; set; } = string.Empty;

    [JsonPropertyName("progress")] public double Progress { get; set; }

    [JsonPropertyName("stage")] public string Stage { get; set; } = string.Empty;

    [JsonPropertyName("error")] public string? Error { get; set; }
}

/// <summary>Review state of one film.</summary>
public class MediaStatus
{
    [JsonPropertyName("path")] public string Path { get; set; } = string.Empty;

    [JsonPropertyName("resolvedPath")] public string? ResolvedPath { get; set; }

    [JsonPropertyName("analyzed")] public bool Analyzed { get; set; }

    [JsonPropertyName("total")] public int Total { get; set; }

    [JsonPropertyName("approved")] public int Approved { get; set; }

    [JsonPropertyName("rejected")] public int Rejected { get; set; }

    [JsonPropertyName("pending")] public int Pending { get; set; }

    [JsonPropertyName("job")] public JobBrief? Job { get; set; }
}

/// <summary>How many jobs a cancel-all request stopped.</summary>
public class CancelAllResult
{
    [JsonPropertyName("cancelled")] public int Cancelled { get; set; }
}

/// <summary>
/// Outcome of asking the worker to render a clean copy. Separates a queued
/// render (<see cref="Job"/>) from a refusal with a reason (<see cref="Error"/>)
/// and an unreachable worker (<see cref="Unreachable"/>).
/// </summary>
public class RenderResult
{
    public WorkerJob? Job { get; init; }

    public bool Unreachable { get; init; }

    /// <summary>The worker's reason for refusing, e.g. nothing approved yet.</summary>
    public string? Error { get; init; }
}

/// <summary>Talks to the Clean Media worker over HTTP.</summary>
public class WorkerClient
{
    private readonly IHttpClientFactory _httpClientFactory;
    private readonly ILogger<WorkerClient> _logger;

    public WorkerClient(IHttpClientFactory httpClientFactory, ILogger<WorkerClient> logger)
    {
        _httpClientFactory = httpClientFactory;
        _logger = logger;
    }

    private static PluginConfiguration Config =>
        Plugin.Instance?.Configuration ?? new PluginConfiguration();

    /// <summary>Fetch the reviewed timeline for a media file, or null if there is none.</summary>
    public async Task<Timeline?> GetTimelineAsync(string mediaPath, CancellationToken cancellationToken)
    {
        var config = Config;
        var url = $"{config.WorkerUrl.TrimEnd('/')}/api/segments"
                  + $"?path={Uri.EscapeDataString(mediaPath)}"
                  + $"&approvedOnly={(config.ApprovedOnly ? "true" : "false")}";

        try
        {
            using var client = _httpClientFactory.CreateClient();
            client.Timeout = TimeSpan.FromSeconds(config.TimeoutSeconds);

            using var response = await client.GetAsync(url, cancellationToken).ConfigureAwait(false);
            if (response.StatusCode == System.Net.HttpStatusCode.NotFound)
            {
                // Perfectly normal: the film simply has not been analyzed.
                return null;
            }

            response.EnsureSuccessStatusCode();
            return await response.Content
                .ReadFromJsonAsync<Timeline>(cancellationToken: cancellationToken)
                .ConfigureAwait(false);
        }
        catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException)
        {
            // A worker that is asleep or unreachable must never break playback
            // or block a library scan.
            _logger.LogWarning(ex, "Clean Media worker unreachable at {Url}", config.WorkerUrl);
            return null;
        }
    }

    private HttpClient NewClient(int? timeoutSeconds = null)
    {
        var client = _httpClientFactory.CreateClient();
        client.Timeout = TimeSpan.FromSeconds(timeoutSeconds ?? Config.TimeoutSeconds);
        return client;
    }

    private static string Base => Config.WorkerUrl.TrimEnd('/');

    private static string SegmentUrl(string mediaPath, int? segmentId = null)
    {
        var id = segmentId is null ? string.Empty : $"/{segmentId}";
        return $"{Base}/api/segments{id}?path={Uri.EscapeDataString(mediaPath)}";
    }

    /// <summary>
    /// Every finding for a film, reviewed or not — what the editor shows. The
    /// result flags an unreachable worker separately from a not-yet-analyzed
    /// film so the page never mistakes one for the other.
    /// </summary>
    public async Task<FindingsResult> GetFindingsAsync(string mediaPath, CancellationToken cancellationToken)
    {
        try
        {
            using var client = NewClient();
            using var response = await client
                .GetAsync(
                    $"{Base}/api/segments?path={Uri.EscapeDataString(mediaPath)}&approvedOnly=false",
                    cancellationToken)
                .ConfigureAwait(false);
            if (response.StatusCode == System.Net.HttpStatusCode.NotFound)
            {
                return new FindingsResult();  // reachable, just not analyzed yet
            }

            response.EnsureSuccessStatusCode();
            var timeline = await response.Content
                .ReadFromJsonAsync<Timeline>(cancellationToken: cancellationToken)
                .ConfigureAwait(false);
            return new FindingsResult { Timeline = timeline };
        }
        catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException)
        {
            _logger.LogWarning(ex, "Clean Media worker unreachable at {Url}", Config.WorkerUrl);
            return new FindingsResult { Unreachable = true };
        }
    }

    /// <summary>Approve, reject or retime a finding. Returns the updated timeline.</summary>
    public async Task<Timeline?> PatchSegmentAsync(
        string mediaPath, int segmentId, object patch, CancellationToken cancellationToken)
    {
        using var client = NewClient();
        using var request = new HttpRequestMessage(HttpMethod.Patch, SegmentUrl(mediaPath, segmentId))
        {
            Content = JsonContent.Create(patch),
        };
        using var response = await client.SendAsync(request, cancellationToken).ConfigureAwait(false);
        response.EnsureSuccessStatusCode();
        return await response.Content
            .ReadFromJsonAsync<Timeline>(cancellationToken: cancellationToken)
            .ConfigureAwait(false);
    }

    /// <summary>Add a finding the engines missed.</summary>
    public async Task<TimelineSegment?> CreateSegmentAsync(
        string mediaPath, object segment, CancellationToken cancellationToken)
    {
        using var client = NewClient();
        using var response = await client
            .PostAsJsonAsync(SegmentUrl(mediaPath), segment, cancellationToken)
            .ConfigureAwait(false);
        response.EnsureSuccessStatusCode();
        return await response.Content
            .ReadFromJsonAsync<TimelineSegment>(cancellationToken: cancellationToken)
            .ConfigureAwait(false);
    }

    /// <summary>Delete a finding outright.</summary>
    public async Task<Timeline?> DeleteSegmentAsync(
        string mediaPath, int segmentId, CancellationToken cancellationToken)
    {
        using var client = NewClient();
        using var response = await client
            .DeleteAsync(SegmentUrl(mediaPath, segmentId), cancellationToken)
            .ConfigureAwait(false);
        response.EnsureSuccessStatusCode();
        return await response.Content
            .ReadFromJsonAsync<Timeline>(cancellationToken: cancellationToken)
            .ConfigureAwait(false);
    }

    /// <summary>Queue a film for analysis.</summary>
    public async Task<WorkerJob?> SubmitJobAsync(
        string mediaPath, string engine, CancellationToken cancellationToken)
    {
        using var client = NewClient();
        using var response = await client
            .PostAsJsonAsync(
                $"{Base}/api/jobs",
                new { mediaPath, engine },
                cancellationToken)
            .ConfigureAwait(false);
        response.EnsureSuccessStatusCode();
        return await response.Content
            .ReadFromJsonAsync<WorkerJob>(cancellationToken: cancellationToken)
            .ConfigureAwait(false);
    }

    /// <summary>
    /// Render a clean copy from a film's approved findings, addressed by media
    /// path. The worker reads the reviewed sidecar and acts only on approved
    /// mutes/blurs/skips; the returned job is polled for progress.
    ///
    /// A refusal (nothing approved yet, or the film is not analyzed) comes back
    /// as <see cref="RenderResult.Error"/> carrying the worker's own reason, so
    /// the page can say why rather than "failed"; an unreachable worker sets
    /// <see cref="RenderResult.Unreachable"/>.
    /// </summary>
    public async Task<RenderResult> RenderAsync(string mediaPath, CancellationToken cancellationToken)
    {
        try
        {
            using var client = NewClient();
            var url = $"{Base}/api/render?path={Uri.EscapeDataString(mediaPath)}";
            using var response = await client.PostAsync(url, null, cancellationToken).ConfigureAwait(false);
            if (response.IsSuccessStatusCode)
            {
                var job = await response.Content
                    .ReadFromJsonAsync<WorkerJob>(cancellationToken: cancellationToken)
                    .ConfigureAwait(false);
                return new RenderResult { Job = job };
            }

            var detail = await ReadDetailAsync(response, cancellationToken).ConfigureAwait(false);
            return new RenderResult
            {
                Error = detail ?? $"the worker refused the render (HTTP {(int)response.StatusCode}).",
            };
        }
        catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException)
        {
            _logger.LogWarning(ex, "Clean Media render request failed at {Url}", Config.WorkerUrl);
            return new RenderResult { Unreachable = true };
        }
    }

    /// <summary>Pull FastAPI's { "detail": "…" } message off an error response.</summary>
    private static async Task<string?> ReadDetailAsync(
        HttpResponseMessage response, CancellationToken cancellationToken)
    {
        try
        {
            var body = await response.Content
                .ReadFromJsonAsync<Dictionary<string, JsonElement>>(cancellationToken: cancellationToken)
                .ConfigureAwait(false);
            if (body is not null
                && body.TryGetValue("detail", out var detail)
                && detail.ValueKind == JsonValueKind.String)
            {
                return detail.GetString();
            }
        }
        catch (Exception ex) when (ex is JsonException or NotSupportedException or HttpRequestException)
        {
            // A non-JSON error body just means no reason to show.
        }

        return null;
    }

    /// <summary>One job's current state, for polling render/analysis progress.</summary>
    public async Task<WorkerJob?> GetJobAsync(string jobId, CancellationToken cancellationToken)
    {
        try
        {
            using var client = NewClient();
            using var response = await client
                .GetAsync($"{Base}/api/jobs/{Uri.EscapeDataString(jobId)}", cancellationToken)
                .ConfigureAwait(false);
            if (response.StatusCode == System.Net.HttpStatusCode.NotFound)
            {
                return null;
            }

            response.EnsureSuccessStatusCode();
            return await response.Content
                .ReadFromJsonAsync<WorkerJob>(cancellationToken: cancellationToken)
                .ConfigureAwait(false);
        }
        catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException)
        {
            _logger.LogWarning(ex, "Clean Media worker unreachable at {Url}", Config.WorkerUrl);
            return null;
        }
    }

    /// <summary>Every job the worker knows about, or null if it is unreachable.</summary>
    public async Task<List<WorkerJob>?> ListJobsAsync(CancellationToken cancellationToken)
    {
        try
        {
            using var client = NewClient();
            return await client
                .GetFromJsonAsync<List<WorkerJob>>($"{Base}/api/jobs", cancellationToken)
                .ConfigureAwait(false);
        }
        catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException)
        {
            _logger.LogWarning(ex, "Clean Media worker unreachable at {Url}", Config.WorkerUrl);
            return null;
        }
    }

    /// <summary>
    /// A browser-playable clip around a finding, for sources the dashboard
    /// player cannot decode itself (DVD rips are MPEG-2, and MKV is not a
    /// container browsers agree on).
    /// </summary>
    public async Task<(Stream Content, string ContentType)?> GetClipAsync(
        string mediaPath, long startMs, long endMs, CancellationToken cancellationToken)
    {
        try
        {
            // Building a clip shells out to ffmpeg; the default timeout is
            // tuned for JSON calls and is far too short for it.
            var client = NewClient(Math.Max(Config.TimeoutSeconds, 120));
            var url = $"{Base}/api/clip?path={Uri.EscapeDataString(mediaPath)}"
                      + $"&startMs={startMs}&endMs={endMs}";
            var response = await client
                .GetAsync(url, HttpCompletionOption.ResponseHeadersRead, cancellationToken)
                .ConfigureAwait(false);
            if (!response.IsSuccessStatusCode)
            {
                return null;
            }

            var stream = await response.Content.ReadAsStreamAsync(cancellationToken)
                .ConfigureAwait(false);
            return (stream, response.Content.Headers.ContentType?.ToString() ?? "video/mp4");
        }
        catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException)
        {
            _logger.LogWarning(ex, "Clean Media clip request failed");
            return null;
        }
    }

    /// <summary>Cancel or forget a job.</summary>
    public async Task<bool> DeleteJobAsync(string jobId, CancellationToken cancellationToken)
    {
        using var client = NewClient();
        using var response = await client
            .DeleteAsync($"{Base}/api/jobs/{Uri.EscapeDataString(jobId)}", cancellationToken)
            .ConfigureAwait(false);
        return response.IsSuccessStatusCode;
    }

    /// <summary>
    /// Cancel every queued or running job. Returns how many were cancelled, or
    /// null if the worker could not be reached.
    /// </summary>
    public async Task<int?> CancelAllJobsAsync(CancellationToken cancellationToken)
    {
        try
        {
            using var client = NewClient();
            using var response = await client
                .PostAsync($"{Base}/api/jobs/cancel-all", null, cancellationToken)
                .ConfigureAwait(false);
            response.EnsureSuccessStatusCode();
            var result = await response.Content
                .ReadFromJsonAsync<CancelAllResult>(cancellationToken: cancellationToken)
                .ConfigureAwait(false);
            return result?.Cancelled ?? 0;
        }
        catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException)
        {
            _logger.LogWarning(ex, "Clean Media worker unreachable at {Url}", Config.WorkerUrl);
            return null;
        }
    }

    /// <summary>Review state for a page of the library, or null if the worker is unreachable.</summary>
    public async Task<List<MediaStatus>?> GetStatusAsync(
        IEnumerable<string> mediaPaths,
        CancellationToken cancellationToken)
    {
        var config = Config;
        try
        {
            using var client = _httpClientFactory.CreateClient();
            // Status walks the media roots to resolve each path, so it needs
            // more headroom than a single-file lookup.
            client.Timeout = TimeSpan.FromSeconds(Math.Max(config.TimeoutSeconds, 30));

            using var response = await client
                .PostAsJsonAsync(
                    $"{config.WorkerUrl.TrimEnd('/')}/api/status",
                    new { paths = mediaPaths },
                    cancellationToken)
                .ConfigureAwait(false);
            response.EnsureSuccessStatusCode();
            return await response.Content
                .ReadFromJsonAsync<List<MediaStatus>>(cancellationToken: cancellationToken)
                .ConfigureAwait(false);
        }
        catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException)
        {
            _logger.LogWarning(ex, "Clean Media worker unreachable at {Url}", config.WorkerUrl);
            return null;
        }
    }

    /// <summary>Fetch worker health, or null if it cannot be reached.</summary>
    public async Task<WorkerHealth?> GetHealthAsync(CancellationToken cancellationToken)
    {
        try
        {
            using var client = _httpClientFactory.CreateClient();
            client.Timeout = TimeSpan.FromSeconds(Config.TimeoutSeconds);
            using var response = await client
                .GetAsync($"{Config.WorkerUrl.TrimEnd('/')}/api/health", cancellationToken)
                .ConfigureAwait(false);
            if (!response.IsSuccessStatusCode)
            {
                return null;
            }

            return await response.Content
                .ReadFromJsonAsync<WorkerHealth>(cancellationToken: cancellationToken)
                .ConfigureAwait(false);
        }
        catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException or NotSupportedException)
        {
            _logger.LogWarning(ex, "Clean Media worker health check failed");
            return null;
        }
    }
}
