using MediaBrowser.Model.Plugins;

namespace Jellyfin.Plugin.CleanMedia.Configuration;

/// <summary>Administrator settings for the Clean Media plugin.</summary>
public class PluginConfiguration : BasePluginConfiguration
{
    /// <summary>Base URL of the Clean Media worker, e.g. http://100.x.y.z:8765.</summary>
    public string WorkerUrl { get; set; } = "http://localhost:8765";

    /// <summary>Seconds to wait for the worker before giving up on an item.</summary>
    public int TimeoutSeconds { get; set; } = 15;

    /// <summary>
    /// Report skip segments to Jellyfin. Mute and blur have no native client
    /// action, so they are never reported as media segments.
    /// </summary>
    public bool ProvideSkips { get; set; } = true;

    /// <summary>
    /// Only surface segments an administrator has explicitly approved.
    /// Turning this off would skip unreviewed AI guesses during playback.
    /// </summary>
    public bool ApprovedOnly { get; set; } = true;
}
