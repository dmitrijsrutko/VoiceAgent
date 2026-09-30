# A listening session that ends, and the next one opening on what the first
# heard. Live, 2026-09-30: a sentence of Russian was decoded as Turkish
# ("Karandaz oğğl, lütshe çem ruchka") because the recognizer was guessing —
# and the guess it settled on belonged to a different conversation, since the
# adapter is one instance shared by the whole server. One session guesses, so
# the tape has one; every later one is narrowed to what was heard, and pinned
# only once the same language comes back a second time.
greeting Hi.
ladder off
reply ручка = Карандаш лучше, потому что им можно писать и стирать.
reply карандаш = Им можно писать и стирать.
0.2 listen
1.0 voice on
1.6 partial Карандаш лучше
2.4 partial Карандаш лучше чем ручка
3.0 voice off
4.0 final Карандаш лучше чем ручка. lang=rus
6.0 unlisten
7.0 listen
8.0 voice on
8.6 partial Караңдаш
9.4 voice off
10.4 final Караңдаш. lang=rus
13.0 partial Карандаш.
14.0 voice off
15.0 final Карандаш. lang=rus
18.0 unlisten
25 end
