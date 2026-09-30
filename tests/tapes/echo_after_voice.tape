# Its own voice, committed after it stopped playing: a second device, or a
# speaker's tail. Nothing was heard over the voice, so no barge-in was judged;
# the committed turn is still its own words and must not be answered. A real
# reply that quotes a few of those words, with its own, is still answered; so
# is a short one made only of the reply's words, not in its order; and so is
# the reply's own line once the voice is long over.
greeting Hi.
ladder off
reply Latvia = Riga is the capital of Latvia. It sits on the Baltic Sea, at the mouth of the Daugava river.
reply wrong = Fair: the Gulf of Riga is an arm of the Baltic.
reply Riga is the Baltic = Mostly, yes: the city sits on the gulf, and the gulf is the Baltic.
0.2 listen
1.0 voice on
2.0 partial what is the capital of Latvia
2.4 voice off
3.4 final What is the capital of Latvia?
10.0 voice on
10.3 partial It sits on the Baltic
10.9 voice off
11.8 final It sits on the Baltic Sea, at the mouth of the Daugava river.
16.0 voice on
16.5 partial No I think you are wrong
17.4 voice off
18.2 final No, I think you are wrong about the Baltic Sea.
23.0 voice on
23.4 voice off
24.2 final Riga is the Baltic.
40.0 voice on
41.5 voice off
42.3 final The city sits on the gulf, and the gulf is the Baltic.
50 end
