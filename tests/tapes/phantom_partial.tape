# The recognizer invents words from the faint echo a phone's canceller leaves
# ("네.", "Я так думаю." — seen live), while the voice detector hears nobody:
# not an interruption. Words with a voice behind them still cut in.
greeting Hi.
ladder off
reply Latvia = Riga is the capital of Latvia. It sits on the Baltic Sea, at the mouth of the Daugava river. Its old town is a World Heritage site, and its Art Nouveau quarter is the largest in Europe.
reply Estonia = Tallinn.
0.2 listen
1.0 voice on
1.4 partial what is the
2.0 partial what is the capital of Latvia
2.4 voice off
3.4 final What is the capital of Latvia?
5.0 partial 네.
6.2 partial Я так думаю.
8.5 voice on
8.9 partial and Estonia
9.3 voice off
10.3 final And Estonia?
18 end
