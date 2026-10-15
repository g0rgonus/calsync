% rebase('layout.tpl', title='Fix ' + noun, flash=flash, narrow=True)
% # One event, corrected by a person and kept that way however many times the
% # team app goes on saying otherwise (`overlay.py`).
% #
% # Written for somebody who knows the coach got it wrong and nothing else: no
% # "override", no "upstream", no per-field switches. The form arrives filled
% # in with what the calendar says now; whatever is changed becomes a fix, and
% # whatever is left alone stays the team app's. The handler compares each part
% # against the team app's own value, so leaving a field untouched can never
% # freeze it.

% from bottle import html_escape
% def fix_mark(unit, part=None):
%   was = unit['team_says' if part is None else 'team_says_' + part]
%   now = unit['now' if part is None else 'now_' + part]
%   if unit['fixed'] and was != now:
%     return '<span class="fix"><s class="fix-was">%s</s> <span class="fix-now">%s</span></span>' % (html_escape(was or 'none'), html_escape(now or 'none'))
%   end
%   return html_escape(now or 'none')
% end

<h1>{{ title }}</h1>
<p class="lede">{{ when_text }}, for {{ team_text }}.</p>

% for alert in alerts:
%   if alert['kind'] == 'changed':
<div class="question fix-alert">
  <h3>{{ app }} changed the {{ alert['label'] }}.</h3>
  <p>It now says <strong>{{ alert['theirs'] }}</strong>. You had fixed it to
     <strong>{{ alert['mine'] }}</strong>, and the calendar still shows yours.
     Which is right?</p>
  <form method="post" action="/events/fix/resolve" class="btn-row">
    <input type="hidden" name="uid" value="{{ uid }}">
    <input type="hidden" name="unit" value="{{ alert['unit'] }}">
    <input type="hidden" name="back" value="{{ back }}">
    <button class="btn" type="submit" name="keep" value="mine">Keep {{ alert['mine'] }}</button>
    <button class="btn btn-answer" type="submit" name="keep" value="theirs">Use {{ alert['theirs'] }}</button>
  </form>
</div>
%   elif alert['kind'] == 'cleared':
<div class="banner banner-good">
  <strong>{{ app }} caught up.</strong> It now says {{ alert['theirs'] }} for the
  {{ alert['label'] }}, the same as your fix, so the fix is no longer needed and
  was removed. Nothing on the calendar changed.
</div>
%   end
% end

% if not upcoming:
<div class="banner banner-info">
  This {{ noun }} has already happened, so it can't be changed now.
</div>
% end

<h2>What everyone sees</h2>
<div class="card fix-preview">
  <div class="fix-when">
% if units['time']['all_day']:
    <span class="fix-clock">All day</span>
% else:
    <span class="fix-clock">{{! fix_mark(units['time'], 'clock') }}</span>
% end
    <span class="fix-day">{{! fix_mark(units['time'], 'day') }}</span>
  </div>
  <dl class="fix-facts">
    <dt>Where</dt>
    <dd>{{! fix_mark(units['location']) }}</dd>
% if is_game:
    <dt>Playing</dt>
    <dd>{{! fix_mark(units['opponent']) }}</dd>
% end
% if units['detail']['now'] or units['detail']['fixed']:
    <dt>Called</dt>
    <dd>{{! fix_mark(units['detail']) }}</dd>
% end
% if units['notes']['now'] or units['notes']['fixed']:
    <dt>Notes</dt>
    <dd class="fix-notes">{{ units['notes']['now'] or 'none' }}</dd>
% end
  </dl>
% # Grouped by save: one change of time and place is one sentence, not two
% # lines that say the same thing about the same person.
% saves = {}
% for unit in units.values():
%   if unit['fixed']:
%     saves.setdefault((unit['by'] or 'someone', unit['on'], unit['reason']), []).append(unit['label'])
%   end
% end
% if saves:
  <p class="note fix-who">
%   for (by, on, reason), labels in saves.items():
%     what = labels[0] if len(labels) == 1 else ', '.join(labels[:-1]) + ' and ' + labels[-1]
    {{ what.capitalize() }} changed by {{ by }} on {{ on }}: “{{ reason }}”<br>
%   end
  </p>
% else:
  <p class="note fix-who">Everything here is what {{ app }} says.</p>
% end
</div>

% if upcoming:
<h2>Change what’s wrong</h2>
<form method="post" action="/events/fix" class="card fix-form">
  <input type="hidden" name="uid" value="{{ uid }}">
  <input type="hidden" name="back" value="{{ back }}">

% def fixed_line(unit):
%   if unit['fixed']:
  <p class="fix-status">
    <span class="tag tag-live">your fix</span>
%     if unit['key'] == 'notes':
    {{ app }}’s notes are different.
%     else:
    {{ app }} says {{ unit['team_says'] or 'nothing' }}.
%     end
    <button class="linkish" type="submit" formaction="/events/fix/undo" formnovalidate
            name="unit" value="{{ unit['key'] }}">Use {{ app }}’s instead</button>
  </p>
%   else:
  <p class="fix-status">Same as {{ app }}.</p>
%   end
% end

  <fieldset class="fix-row">
    <legend>When is it?</legend>
%   fixed_line(units['time'])
    <div class="fix-inputs">
      <label class="fix-in fix-in-date">Day
        <input type="date" name="date" value="{{ form['date'] }}" required></label>
      <label class="fix-in">Starts
        <input type="time" name="start" value="{{ form['start'] }}"></label>
      <label class="fix-in">Ends
        <input type="time" name="end" value="{{ form['end'] }}"></label>
    </div>
    <label class="fix-check">
      <input type="checkbox" name="all_day" value="1" {{ 'checked' if form['all_day'] else '' }}>
      All day, no time yet
    </label>
  </fieldset>

  <fieldset class="fix-row">
    <legend>Where is it?</legend>
%   fixed_line(units['location'])
    <div class="fix-inputs">
      <label class="fix-in fix-in-wide">Place
        <input type="text" name="venue_name" value="{{ form['venue_name'] }}"
               list="known-venues" autocomplete="off"></label>
      <label class="fix-in fix-in-short">Field
        <input type="text" name="venue_field" value="{{ form['venue_field'] }}"
               placeholder="#2"></label>
    </div>
    <datalist id="known-venues">
% for name in venues:
      <option value="{{ name }}">
% end
    </datalist>
    <label class="fix-in">Address <span class="fix-opt">only for a place not in the list</span>
      <input type="text" name="venue_address" value="{{ form['venue_address'] }}"></label>
  </fieldset>

% if is_game:
  <fieldset class="fix-row">
    <legend>Who are they playing?</legend>
%   fixed_line(units['opponent'])
    <input type="text" name="opponent" value="{{ form['opponent'] }}" aria-label="Opponent">
  </fieldset>
% end

  <fieldset class="fix-row">
    <legend>What’s it called?</legend>
%   fixed_line(units['detail'])
    <input type="text" name="detail" value="{{ form['detail'] }}" aria-label="Name"
           placeholder="Skills session, Semifinal">
  </fieldset>

  <fieldset class="fix-row">
    <legend>Notes</legend>
%   fixed_line(units['notes'])
    <p class="note fix-hint">These start as the coach’s notes, so you can add to them
       or correct them rather than retype them.</p>
    <textarea name="notes" rows="4" aria-label="Notes">{{ form['notes'] }}</textarea>
  </fieldset>

  <div class="fix-sign">
    <label class="fix-in fix-in-wide">Why are you changing it?
      <input type="text" name="reason" required placeholder="Coach texted: kickoff moved to 11"></label>
    <label class="fix-in fix-in-name">Your name
      <input type="text" name="actor" value="{{ actor }}" autocomplete="name"></label>
  </div>

  <div class="btn-row fix-go">
    <button class="btn" type="submit">Update the calendar</button>
    <a class="btn btn-quiet" href="{{ back }}">Cancel</a>
  </div>
  <p class="note">Your changes stay put, even when {{ app }} still has the old
     details. If {{ app }} later says the same thing as your fix, the fix is
     removed on its own; if it says something different, you’ll be asked which
     is right.</p>
</form>
% end
