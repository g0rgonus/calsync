% rebase('layout.tpl', title='Household', flash=flash, narrow=True)

<p class="eyebrow">Rarely, and then never again</p>
<h1>Household</h1>
<p class="lede">
  Kids and sports change about once a lifetime and about once a year
  respectively. Teams are the part that churns, and they live on
  <a href="/">Teams</a>.
</p>

<h2>Kids</h2>
<div class="stack">
% for entry in children:
%   child = entry['row']
%   usage = entry['usage']
  <div class="card" id="{{ child['id'] }}">
    <form method="post" action="/children">
      <input type="hidden" name="id" value="{{ child['id'] }}">
      <div class="row">
        <label class="field" style="margin-bottom:0.8rem">
          <span class="label">Name</span>
          <input type="text" name="name" value="{{ child['name'] }}" required autocomplete="off">
        </label>
        <label class="field" style="margin-bottom:0.8rem;flex:0 1 6rem">
          <span class="label">Initial</span>
          <input type="text" name="initial" value="{{ child['initial'] }}" maxlength="2"
                 class="mono" required autocomplete="off">
        </label>
        <label class="field" style="margin-bottom:0.8rem;flex:0 1 7rem">
          <span class="label">List order</span>
          <input type="number" name="birth_order" value="{{ child['birth_order'] }}" min="1">
        </label>
      </div>
      <div class="row">
        <label class="field" style="margin-bottom:0.8rem">
          <span class="label">Also known as</span>
          <input type="text" name="nicknames" autocomplete="off"
                 value="{{ entry['nicknames'] }}"
                 placeholder="comma separated">
        </label>
        <label class="field" style="margin-bottom:0.8rem;flex:0 1 8rem">
          <span class="label">Colour</span>
          <input type="text" name="color" class="mono" value="{{ child['color'] or '' }}"
                 placeholder="#2f6fdb" autocomplete="off">
        </label>
      </div>

      <div class="btn-row">
        <button class="btn" type="submit">Save {{ child['name'] }}</button>
%       if usage.activities:
        <span class="note">
          {{ len(usage.activities) }}
          {{ 'team' if len(usage.activities) == 1 else 'teams' }}:
          {{ ', '.join(usage.activities) }} · {{ usage.tracked_events }} events on the calendar
        </span>
%       else:
        <span class="note">No teams yet.</span>
%       end
      </div>
    </form>

%   if not usage.activities:
    <form method="post" action="/children/{{ child['id'] }}/delete" style="margin-top:0.7rem">
      <button class="btn btn-danger" type="submit">Remove {{ child['name'] }}</button>
    </form>
%   end
  </div>
% end
</div>

<div class="card" style="margin-top:0.9rem">
  <p class="label" style="margin-bottom:0.7rem">Add a kid</p>
  <form method="post" action="/children">
    <div class="row">
      <label class="field" style="margin-bottom:0.8rem">
        <span class="label">Name</span>
        <input type="text" name="name" required autocomplete="off">
      </label>
      <label class="field" style="margin-bottom:0.8rem;flex:0 1 6rem">
        <span class="label">Initial</span>
        <input type="text" name="initial" maxlength="2" class="mono" autocomplete="off">
      </label>
    </div>
    <button class="btn" type="submit">Add</button>
  </form>
  <p class="note" style="margin-bottom:0">
    Initials have to be unique. Nothing renders one today — an activity belongs
    to exactly one kid, so every title carries a full name — but the constraint
    is there for when two kids share a team and the title collapses to
    <span class="raw">P+J</span>, which only reads unambiguously if no two kids
    are <span class="raw">P</span>. List order sorts this page and the dropdowns,
    and will fix which way round that pair renders.
  </p>
</div>

<h2>Sports</h2>
<div class="card">
  <p class="note">
    A sport's emoji is in the title of every one of its events, so it is worth
    getting right once. Built-in sports can be re-emoji'd and the edit survives
    an upgrade; they cannot be deleted, because the seed list is reapplied on
    every migration and they would simply come back.
  </p>

% def sport_row(entry):
%   sport, usage = entry['row'], entry['usage']
  <div class="sport" id="sport-{{ sport['id'] }}">
    <form method="post" action="/sports" class="sport-form">
      <input type="hidden" name="id" value="{{ sport['id'] }}">
      <input type="text" name="emoji" value="{{ sport['emoji'] }}" class="sport-emoji"
             aria-label="emoji for {{ sport['name'] }}">
      <input type="text" name="name" value="{{ sport['name'] }}" class="sport-name"
             aria-label="name of {{ sport['id'] }}">
      <button class="btn btn-quiet" type="submit">Save</button>
    </form>
%   if usage.activities:
    <span class="sport-use">{{ ', '.join(usage.activities) }}</span>
%   elif not sport['builtin']:
    <form method="post" action="/sports/{{ sport['id'] }}/delete">
      <button class="btn btn-danger" type="submit">Remove</button>
    </form>
%   end
  </div>
% end

% in_use = [e for e in sports if e['usage'].activities or not e['row']['builtin']]
% rest = [e for e in sports if e not in in_use]

% for entry in in_use:
%   sport_row(entry)
% end

% if rest:
  <details class="more">
    <summary>{{ len(rest) }} more built-in sports</summary>
%   for entry in rest:
%     sport_row(entry)
%   end
  </details>
% end
</div>

<div class="card" style="margin-top:0.9rem">
  <p class="label" style="margin-bottom:0.7rem">Add a sport</p>
  <form method="post" action="/sports">
    <div class="row">
      <label class="field" style="margin-bottom:0.8rem;flex:0 1 6rem">
        <span class="label">Emoji</span>
        <input type="text" name="emoji" required style="text-align:center" autocomplete="off">
      </label>
      <label class="field" style="margin-bottom:0.8rem">
        <span class="label">Name</span>
        <input type="text" name="name" required autocomplete="off" placeholder="Fencing">
      </label>
    </div>
    <button class="btn" type="submit">Add</button>
  </form>
</div>
