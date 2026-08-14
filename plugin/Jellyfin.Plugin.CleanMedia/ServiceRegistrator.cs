using MediaBrowser.Controller;
using MediaBrowser.Controller.MediaSegments;
using MediaBrowser.Controller.Plugins;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;

namespace Jellyfin.Plugin.CleanMedia;

/// <summary>Registers the segment provider with Jellyfin's DI container.</summary>
public class ServiceRegistrator : IPluginServiceRegistrator
{
    /// <inheritdoc />
    public void RegisterServices(IServiceCollection serviceCollection, IServerApplicationHost applicationHost)
    {
        serviceCollection.AddHttpClient();
        serviceCollection.AddSingleton<WorkerClient>();
        serviceCollection.AddSingleton<IMediaSegmentProvider, CleanMediaSegmentProvider>();

        // Patches index.html on startup to add the in-player "flag this moment"
        // button. Runs every launch so a Jellyfin upgrade cannot leave it out.
        serviceCollection.AddHostedService<WebInjectionService>();
    }
}
