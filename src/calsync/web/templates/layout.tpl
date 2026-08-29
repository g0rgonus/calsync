% setdefault('title', 'calsync')
% setdefault('flash', None)
% setdefault('narrow', False)
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="same-origin">
<title>{{ title }} — calsync</title>
<link rel="icon" type="image/svg+xml" href="/static/icon.svg">
<link rel="icon" type="image/png" sizes="32x32" href="/static/icon-32.png">
<link rel="apple-touch-icon" href="/static/icon-180.png">
<link rel="stylesheet" href="/static/app.css">
</head>
<body>

<header class="rail">
  <div class="rail-in">
    <a class="wordmark" href="/">calsync</a>
    <span class="wordmark-sub">console</span>
    <!-- The phone menu's control, and the console's only stateful widget.
         A checkbox because there is no JavaScript here and this is not the
         thing to add some for; a bare checkbox rather than a hidden one behind
         a <label> because then it is focusable, toggles on Space and carries
         its own name with nothing bolted on. Why not <details>, and why
         `autocomplete`, are both in app.css beside the styles. -->
    <input class="rail-burger" type="checkbox" aria-label="Menu" autocomplete="off">
    <nav class="rail-nav">
      <a href="/">Teams</a>
      <a href="/calendar">Calendar</a>
      <a href="/onboard">Add a team</a>
      <a href="/review">Review</a>
      <a href="/venues">Venues</a>
      <a href="/household">Household</a>
      <a href="/settings">Settings</a>
    </nav>
  </div>
</header>

<main class="page {{ 'page-narrow' if narrow else '' }}">
% if flash:
  <div class="banner banner-{{ flash['kind'] }}">{{ flash['text'] }}</div>
% end
{{! base }}
</main>

<footer class="foot">
  <span>calsync {{ version }}</span>
</footer>

</body>
</html>
