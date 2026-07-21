// 10.11 relocated the enum here from Jellyfin.Data.Enums.
using Jellyfin.Database.Implementations.Enums;
using MediaBrowser.Controller;
// 10.11 moved this out of MediaBrowser.Controller into its own namespace.
using MediaBrowser.Controller.MediaSegments;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Entities.Movies;
using MediaBrowser.Controller.Entities.TV;
using MediaBrowser.Model;
using MediaBrowser.Model.MediaSegments;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.CleanMedia;

/// <summary>
/// Supplies Clean Media's approved skip segments to Jellyfin.
///
/// Jellyfin's MediaSegmentType enum is fixed at six values and none of them
/// mean "objectionable content", so approved skips are reported as
/// <see cref="MediaSegmentType.Commercial"/> — the type clients are most
/// willing to skip automatically.
///
/// Only skips can be expressed here. Jellyfin has no native action for
/// muting or blurring a range, so those segments are deliberately not
/// reported: a client told to *skip* a mute segment would jump over
/// dialogue the administrator only wanted silenced.
/// </summary>
public class CleanMediaSegmentProvider : IMediaSegmentProvider
{
    private readonly WorkerClient _worker;
    private readonly ILogger<CleanMediaSegmentProvider> _logger;

    public CleanMediaSegmentProvider(WorkerClient worker, ILogger<CleanMediaSegmentProvider> logger)
    {
        _worker = worker;
        _logger = logger;
    }

    /// <inheritdoc />
    public string Name => "Clean Media";

    /// <inheritdoc />
    public ValueTask<bool> Supports(BaseItem item)
        => ValueTask.FromResult(item is Movie or Episode && !string.IsNullOrEmpty(item.Path));

    /// <inheritdoc />
    public async Task<IReadOnlyList<MediaSegmentDto>> GetMediaSegments(
        MediaSegmentGenerationRequest request,
        CancellationToken cancellationToken)
    {
        var config = Plugin.Instance?.Configuration;
        if (config is null || !config.ProvideSkips)
        {
            return Array.Empty<MediaSegmentDto>();
        }

        var item = Plugin.LibraryManager?.GetItemById(request.ItemId);
        if (item is null || string.IsNullOrEmpty(item.Path))
        {
            return Array.Empty<MediaSegmentDto>();
        }

        var timeline = await _worker.GetTimelineAsync(item.Path, cancellationToken).ConfigureAwait(false);
        if (timeline is null)
        {
            return Array.Empty<MediaSegmentDto>();
        }

        var segments = timeline.Segments
            .Where(s => string.Equals(s.RecommendedAction, "skip", StringComparison.OrdinalIgnoreCase))
            .Where(s => s.EndMs > s.StartMs)
            .Select(s => new MediaSegmentDto
            {
                ItemId = request.ItemId,
                Type = MediaSegmentType.Commercial,
                StartTicks = TimeSpan.FromMilliseconds(s.StartMs).Ticks,
                EndTicks = TimeSpan.FromMilliseconds(s.EndMs).Ticks,
            })
            .ToList();

        var skipped = timeline.Segments.Count - segments.Count;
        if (skipped > 0)
        {
            _logger.LogInformation(
                "Clean Media: {Skip} skip segment(s) for {Name}; {Other} mute/blur segment(s) "
                + "cannot be applied during playback and need a rendered copy",
                segments.Count, item.Name, skipped);
        }

        return segments;
    }
}
