% # The confirmation for one settings section, rendered beside the form that
% # produced it rather than at the top of the page.
% #
% # A partial for the same reason `_answers.tpl` is one: seven sections each
% # hand-rolling this is seven chances for one of them to word its banner
% # differently or drop it entirely on a busy afternoon.
% #
% # `at` is the section the redirect named. A save with no section — anything
% # that still redirects to a bare `/settings` — leaves every one of these
% # silent and falls through to the banner `layout.tpl` draws at the top, so
% # a confirmation is never lost, only relocated when there is somewhere
% # better to put it.
% if flash and at == section:
<div class="banner banner-{{ flash['kind'] }}">{{ flash['text'] }}</div>
% end
