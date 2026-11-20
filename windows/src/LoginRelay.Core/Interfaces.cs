// SPDX-License-Identifier: GPL-3.0-or-later
using System;

namespace LoginRelay.Core
{
    /// <summary>
    /// Runs an action on the UI thread. The engine's state is only ever touched there.
    /// Must be safe to call from any thread, and must not throw once the UI is gone.
    /// </summary>
    public interface IUiDispatcher
    {
        void Post(Action action);
    }

    /// <summary>A temporary claim on a URL scheme. Release puts the previous handler back. Idempotent.</summary>
    public interface IClaim
    {
        void Release();
    }

    /// <summary>A modeless prompt the engine can dismiss when the login ends some other way.</summary>
    public interface IPromptHandle
    {
        /// <summary>Closes the prompt without reporting a cancel.</summary>
        void Close();
    }

    /// <summary>
    /// Everything the engine needs from the operating system and the user interface. All members are
    /// called on the UI thread. The engine never touches WinForms or the registry directly.
    /// </summary>
    public interface IPlatform
    {
        /// <summary>Opens an address in the default browser. Returns false if it couldn't (or wouldn't).</summary>
        bool OpenUrl(string url);

        /// <summary>
        /// Makes this app the handler for <paramref name="scheme"/> until the claim is released. Returns
        /// null if the system refuses, so the caller can fall back to pasting the address.
        /// </summary>
        IClaim? Claim(string scheme);

        /// <summary>A passing notification ("balloon"). The engine has already checked the user's preference.</summary>
        void Notify(string title, string body);

        void ShowError(string title, string detail);

        /// <summary>"X is signing in without PKCE": true to continue anyway.</summary>
        bool ConfirmWithoutPkce(string appName);

        /// <summary>
        /// Asks the user to paste the final address (for return addresses an app can't be handed directly).
        /// <paramref name="check"/> is called with what the user typed and returns null when it was accepted
        /// (the dialog then closes) or a message to show and keep asking. <paramref name="onCancel"/> runs
        /// if the user gives up. The dialog must not block the UI thread's message loop.
        /// </summary>
        IPromptHandle AskForPastedAddress(string startsWith, Func<string, string?> check, Action onCancel);
    }
}
