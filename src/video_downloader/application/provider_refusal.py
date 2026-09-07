"""What a provider says when the answer is "no", as opposed to "it broke".

Every adapter classifies its own refusals - a profile instead of a post, a live
Space, a post X hands out only to a signed-in viewer, a video whose uploader
disabled downloads - and each of those already carries the sentence a user
reads. Nothing failed: the request was made, the site answered, and the answer
was no. A retry returns the same one.

The download service has to tell those apart from a transport error or an answer
it could not read, and it may not do it by naming provider classes: that
function is the one place which knows nothing about the website a URL belongs
to. So the adapters mark their refusals and it reads the mark, which is all this
module is.

Measured on 2026-09-07, and the reason it exists: a post that simply had no
video was logged as three chained exceptions over fourteen frames, ending in a
sentence that was meant for the user.
"""

from __future__ import annotations


class ProviderRefusal(Exception):
    """A provider's own, deliberate "no" to a URL.

    Mixed into the refusals an adapter classifies itself, next to that adapter's
    own base class rather than instead of it: `except XError` keeps catching
    everything the X adapter can raise, and `except ProviderRefusal` catches the
    refusals of every adapter without naming one.
    """
