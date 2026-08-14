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

    /// <summary>
    /// Show the "flag this moment" button in the video player. It captures a
    /// short window around the current time as an unapproved finding for review.
    /// </summary>
    public bool FlagButtonEnabled { get; set; } = true;

    /// <summary>
    /// Half-width, in milliseconds, of the window one flag press captures: the
    /// finding spans (now - pad) to (now + pad). Retimed precisely in review.
    /// </summary>
    public int FlagPadMs { get; set; } = 1500;
}
