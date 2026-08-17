% rebase('layout.tpl', title='Venues', flash=flash, narrow=True)

<p class="eyebrow">Once per place, forever</p>
<h1>Venues</h1>
<p class="lede">
  Teams are renamed every season; the parks and schools are not. A name added
  here resolves that place for every team that ever plays there, so this list
  gets shorter to maintain each year rather than longer.
</p>



% if not venues:
<div class="empty">
  <p><strong>No venues yet.</strong></p>
  <p>They arrive on their own: when a feed names a place calsync doesn't know,
     the team's page asks about it.</p>
  <a class="btn" href="/">Back to teams</a>
</div>
% else:

<div class="card">
  <table class="derived">
% for venue in venues:
    <tr>
      <th style="width:auto;white-space:normal">
        <a href="/venues/{{ venue.id }}" style="font-size:1rem">{{ venue.name }}</a>
% if venue.home_to:
        <span class="note" style="display:block;text-transform:none;letter-spacing:0">
          home ground for {{ ', '.join(venue.home_to) }}
        </span>
% end
      </th>
      <td>
        {{ len(venue.aliases) }}
        {{ 'name' if len(venue.aliases) == 1 else 'names' }}
% if venue.address:
        <span class="note" style="display:block">{{ venue.address }}</span>
% end
      </td>
    </tr>
% end
  </table>
</div>

% end

<h2>Add a place</h2>
<div class="card">
  <form method="post" action="/venues">
    <div class="row">
      <label class="field" style="margin-bottom:0.8rem">
        <span class="label">Name</span>
        <input type="text" name="name" required autocomplete="off"
               placeholder="Riverview Farm Park">
      </label>
      <label class="field" style="margin-bottom:0.8rem;flex:0 1 9rem">
        <span class="label">Short name</span>
        <input type="text" name="short_name" autocomplete="off">
      </label>
    </div>
    <label class="field" style="margin-bottom:0.8rem">
      <span class="label">Address</span>
      <input type="text" name="address" autocomplete="off">
    </label>
    <button class="btn" type="submit">Add</button>
    <span class="note" style="margin-left:0.6rem">
      Name the place, not the field within it — <span class="raw">Riverview</span>,
      not <span class="raw">Riverview #2</span>.
    </span>
  </form>
</div>
