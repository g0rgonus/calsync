% rebase('layout.tpl', title=venue.name, flash=flash, narrow=True)
% setdefault('pin_source', None)

<p class="eyebrow"><a href="/venues" style="color:inherit">Venues</a></p>
<h1>{{ venue.name }}</h1>
% if venue.home_to:
<p class="lede">Home ground for {{ ', '.join(venue.home_to) }}.</p>
% end

% if venue.proposed:
<div class="banner banner-info">
  <strong>These coordinates are unconfirmed.</strong>
  They came from {{ pin_source or 'a source nothing recorded' }}, and nobody
  has checked them. Look at the pin, then confirm or correct it.
  <form method="post" action="/venues/{{ venue.id }}/confirm" style="margin-top:0.7rem">
    <button class="btn btn-answer" type="submit">These are right</button>
  </form>
</div>
% end

<h2>The place</h2>
<div class="card">
  <form method="post" action="/venues/{{ venue.id }}">
    <div class="row">
      <label class="field" style="margin-bottom:0.8rem">
        <span class="label">Name</span>
        <input type="text" name="name" value="{{ venue.name }}" required autocomplete="off">
      </label>
      <label class="field" style="margin-bottom:0.8rem;flex:0 1 9rem">
        <span class="label">Short name</span>
        <input type="text" name="short_name" value="{{ venue.short_name or '' }}"
               autocomplete="off">
      </label>
    </div>
    <label class="field" style="margin-bottom:0.8rem">
      <span class="label">Address</span>
      <input type="text" name="address" value="{{ venue.address or '' }}" autocomplete="off">
    </label>
    <div class="row">
      <label class="field" style="margin-bottom:0.8rem">
        <span class="label">Latitude</span>
        <input type="text" name="lat" class="mono" autocomplete="off"
               value="{{ '' if venue.lat is None else venue.lat }}">
      </label>
      <label class="field" style="margin-bottom:0.8rem">
        <span class="label">Longitude</span>
        <input type="text" name="lon" class="mono" autocomplete="off"
               value="{{ '' if venue.lon is None else venue.lon }}">
      </label>
    </div>
    <button class="btn" type="submit">Save this place</button>
    <span class="note" style="margin-left:0.6rem">
% if venue.pinned:
      Pin from {{ pin_source or 'a source nothing recorded' }}.
      Editing the coordinates re-confirms them as yours.
% else:
      Coordinates are optional. Without them the location still reads correctly —
      it just isn't tappable, which beats sending someone to the wrong park.
% end
    </span>
  </form>
</div>

<h2>Names seen in feeds</h2>
<div class="card">
  <p class="note">
    Every string here resolves to this place, with no lookup and no guessing.
    This is what makes a venue a one-time cost: coaches type
    <span class="raw">Riverview</span>, <span class="raw">Riverview Farm Park</span>
    and <span class="raw">Riverview#2</span> for the same field, and each one only
    has to be learned once.
  </p>

  <ul class="raw-list">
% for alias in venue.aliases:
    <li>
      <span class="raw">{{ alias }}</span>
% if alias != venue.name:
      <form method="post" action="/venues/{{ venue.id }}/alias" style="display:inline">
        <input type="hidden" name="remove" value="{{ alias }}">
        <button class="btn btn-danger" type="submit"
                style="padding:0.1rem 0.4rem;font-size:0.62rem;letter-spacing:0.06em"
                aria-label="stop {{ alias }} resolving here">drop</button>
      </form>
% end
    </li>
% end
  </ul>

  <form method="post" action="/venues/{{ venue.id }}/alias">
    <label class="field" style="margin-bottom:0.6rem">
      <span class="label">Another name for this place</span>
      <input type="text" name="alias" class="mono" required autocomplete="off"
             placeholder="exactly as it appears in the feed">
    </label>
    <button class="btn btn-answer" type="submit">Add this name</button>
  </form>
</div>

% if others:
<h2>Same place as another?</h2>
<div class="card">
  <p class="note">
    Three coaches typing three names for one park is how this list grows a
    duplicate. Merging keeps every name from both — they are all real strings
    from real feeds — and moves any home-ground setting across. This venue's row
    goes away; nothing on the calendar changes.
  </p>
  <form method="post" action="/venues/{{ venue.id }}/merge">
    <div class="row">
      <label class="field" style="margin-bottom:0.8rem">
        <span class="label">{{ venue.name }} is really</span>
        <select name="into" required>
          <option value="">choose the one to keep…</option>
% for other in others:
          <option value="{{ other.id }}">{{ other.name }}</option>
% end
        </select>
      </label>
    </div>
    <button class="btn btn-quiet" type="submit">Merge into that one</button>
  </form>
</div>
% end

<h2>Remove</h2>
<div class="card">
  <p class="note">
    Safe. Nothing on the calendar points at a venue — events carry theirs by
    value — so removing this only means those events re-render without a pin and
    the name turns up as unresolved on the team's page again.
% if venue.home_to:
    {{ ', '.join(venue.home_to) }} would lose its home ground, which is what
    decides home or away when a feed doesn't say.
% end
  </p>
  <form method="post" action="/venues/{{ venue.id }}/delete">
    <button class="btn btn-danger" type="submit">Remove {{ venue.name }}</button>
  </form>
</div>
