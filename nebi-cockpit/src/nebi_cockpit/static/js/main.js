// Web Cockpit entry point (ES module, no build step). Each feature module binds its own controls on import;
// this file exposes the small window.NebiUI bridge used by the classic scripts (intercom.js, privacy.js) and by
// extension UI modules (see extensions.js), starts the sockets and loads the extension modules.
//
// Module map: core (state, helpers, REST, modals) <- video, connection, telemetry, drive (+ pads),
// catalog, params, media, camera, follow, games, audio, voice (Speak), eyes, system, tabs.

import { loadCatalogAndInit } from "./catalog.js";
import { connectControlWs } from "./connection.js";
import { isTyping, toast } from "./core.js";
import { currentView, loadExtensions, registerTab, registerView, selectView } from "./extensions.js";
import { selectTab } from "./tabs.js";
import { connectVideoWs } from "./video.js";
import "./drive.js";
import "./camera.js";
import "./follow.js";
import "./games.js";
import "./audio.js";
import "./voice.js";
import "./eyes.js";
import "./system.js";

window.NebiUI = { toast, isTyping, selectTab, registerTab, registerView, selectView, currentView };

connectControlWs();
connectVideoWs();
loadCatalogAndInit();
loadExtensions();
