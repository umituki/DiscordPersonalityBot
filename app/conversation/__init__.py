"""Conversation system (spec 16, 35 Phase 3).

Import from the submodules directly (``app.conversation.engine`` etc.). This
package deliberately re-exports nothing: the storage layer needs
``app.conversation.models`` for the turn projection, and eager re-exports here
would drag the engine — and through it the state layer — into that import.
"""
