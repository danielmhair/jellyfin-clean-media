using Jellyfin.Plugin.CleanMedia.Configuration;
using MediaBrowser.Common.Configuration;
using MediaBrowser.Common.Plugins;
using MediaBrowser.Controller.Library;
using MediaBrowser.Model.Plugins;
using MediaBrowser.Model.Serialization;

namespace Jellyfin.Plugin.CleanMedia;

/// <summary>Clean Media plugin entry point.</summary>
public class Plugin : BasePlugin<PluginConfiguration>, IHasWebPages
{
    public Plugin(
        IApplicationPaths applicationPaths,
        IXmlSerializer xmlSerializer,
        ILibraryManager libraryManager)
        : base(applicationPaths, xmlSerializer)
    {
        Instance = this;
        LibraryManager = libraryManager;
    }

    /// <summary>The running plugin instance.</summary>
    public static Plugin? Instance { get; private set; }

    /// <summary>Used to resolve a library item id back to its file path.</summary>
    public static ILibraryManager? LibraryManager { get; private set; }

    /// <inheritdoc />
    public override string Name => "Clean Media";

    /// <inheritdoc />
    public override Guid Id => Guid.Parse("6f1d0a2e-6c2b-4a1f-9a6d-1c5b2f8e4d31");

    /// <inheritdoc />
    public override string Description =>
        "Skips administrator-approved objectionable scenes using a self-hosted Clean Media worker.";

    /// <inheritdoc />
    public IEnumerable<PluginPageInfo> GetPages()
    {
        // One page for everything: a tabbed dashboard with Settings and Queue,
        // so configuration and work management live in the same place — no
        // separate config page. It is the plugin's main-menu page and, in
        // Jellyfin 10.11, also what the plugin's own "Settings" button opens
        // (findBestConfigurationPage prefers the EnableInMainMenu page).
        yield return new PluginPageInfo
        {
            Name = "CleanMediaReview",
            DisplayName = "Clean Media",
            EmbeddedResourcePath = $"{GetType().Namespace}.Configuration.reviewPage.html",
            EnableInMainMenu = true,
            MenuIcon = "content_cut",
        };
    }
}
