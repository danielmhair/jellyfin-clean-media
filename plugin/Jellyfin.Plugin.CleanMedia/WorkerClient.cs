using System.Net.Http.Json;
using System.Text.Json.Serialization;
using Jellyfin.Plugin.CleanMedia.Configuration;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.CleanMedia;

/// <summary>One segment in the worker's engine-agnostic timeline.</summary>
public class TimelineSegment
{
    [JsonPropertyName("startMs")] public long StartMs { get; set; }

    [JsonPropertyName("endMs")] public long EndMs { get; set; }

    [JsonPropertyName("category")] public string Category { get; set; } = string.Empty;

    [JsonPropertyName("confidence")] public double Confidence { get; set; }

    [JsonPropertyName("engine")] public string Engine { get; set; } = string.Empty;

    [JsonPropertyName("recommendedAction")] public string RecommendedAction { get; set; } = string.Empty;

    [JsonPropertyName("approved")] public bool? Approved { get; set; }

    [JsonPropertyName("reasoning")] public string? Reasoning { get; set; }
}

/// <summary>The worker's standard timeline response.</summary>
public class Timeline
{
    [JsonPropertyName("schemaVersion")] public int SchemaVersion { get; set; }

    [JsonPropertyName("mediaFingerprint")] public string MediaFingerprint { get; set; } = string.Empty;

    [JsonPropertyName("segments")] public List<TimelineSegment> Segments { get; set; } = new();
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

    /// <summary>Check worker reachability for the settings page.</summary>
    public async Task<bool> PingAsync(CancellationToken cancellationToken)
    {
        try
        {
            using var client = _httpClientFactory.CreateClient();
            client.Timeout = TimeSpan.FromSeconds(Config.TimeoutSeconds);
            using var response = await client
                .GetAsync($"{Config.WorkerUrl.TrimEnd('/')}/api/health", cancellationToken)
                .ConfigureAwait(false);
            return response.IsSuccessStatusCode;
        }
        catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException)
        {
            _logger.LogWarning(ex, "Clean Media worker health check failed");
            return false;
        }
    }
}
