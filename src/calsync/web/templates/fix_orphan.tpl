% rebase('layout.tpl', title='Where does this fix go?', flash=flash, narrow=True)
% # A fix whose event the team app no longer has. Coaches delete a game and add
% # it again rather than edit it, which mints a new id — so the fix is kept,
% # and a person says where it belongs. calsync suggests and never picks: a
% # wrong guess is a wrong time on a calendar the whole family reads.

<h1>{{ title }} disappeared from {{ app }}</h1>
<p class="lede">It was on {{ when }}. You had fixed its {{ labels }}. If the coach
   added it again, pick it below and your fix moves onto it. calsync won’t
   guess — a wrong guess would put the wrong details on everyone’s calendar.</p>

<h2>Your fix</h2>
<div class="card fix-preview fix-preview-flat">
  <dl class="fix-facts">
% for fix in fixes:
    <dt>{{ fix['label'].capitalize() }}</dt>
    <dd><span class="fix-now">{{ fix['value'] }}</span></dd>
% end
  </dl>
</div>

% if candidates:
<h2>Is it one of these?</h2>
<form method="post" action="/events/fix/reattach" class="card">
  <input type="hidden" name="uid" value="{{ uid }}">
  <input type="hidden" name="back" value="{{ back }}">
% for c in candidates:
  <label class="choice">
    <input type="radio" name="to" value="{{ c['uid'] }}" required>
    <span class="choice-body">
      <span class="choice-title">{{ c['title'] }}</span>
      <span class="choice-note">{{ c['when'] }}{{ (', ' + c['place']) if c['place'] else '' }}</span>
%   if c['fixed_already']:
      <span class="choice-note fix-agrees">{{ App }} already says what your fix says, so you
        may not need it — forgetting it is fine too.</span>
%   end
    </span>
  </label>
% end
  <label class="fix-in fix-in-name">Your name
    <input type="text" name="actor" value="{{ actor }}" autocomplete="name"></label>
  <div class="btn-row fix-go">
    <button class="btn" type="submit">Move my fix there</button>
  </div>
</form>
% else:
<div class="empty">
  <p><strong>Nothing still to come looks like it.</strong></p>
  <p>If the coach hasn’t added it back yet, leave this and look again later. If
     the {{ 'game' if 'opponent' in labels else 'event' }} is really off, forget the fix.</p>
</div>
% end

<form method="post" action="/events/fix/forget" class="btn-row fix-forget">
  <input type="hidden" name="uid" value="{{ uid }}">
  <input type="hidden" name="back" value="{{ back }}">
  <button class="btn btn-quiet" type="submit">Forget my fix</button>
  <span class="note">It’s off the calendar either way; this just stops asking.</span>
</form>
