import sys, time, math, configparser, os, csv
import numpy as np
import tensorflow as tf

from fasttext import load_model
from pprint import pprint
from pathlib import Path
from datetime import date
from sklearn.model_selection import KFold, StratifiedKFold

from models.fasttext_model import baseline as baseline_model
from models.bert_model import bert_model
from models.functional_model import FunctionalModel
from models.tf_model import TfModel
from models.svm import SVM
from data_mgmt.data_mgmt import new_dataset, get_dataset, dataset_to_embeddings, get_bert_token_ids

EPOCHS = 1
BATCH_SIZE = 1

def labelProportion(trainLabels, testLabels):
    countYesTraining = 0
    countYesTest = 0

    countYesTraining = 0
    for label in trainLabels:
        if label == 1:
            countYesTraining += 1

    trainProportion = countYesTraining / len(trainLabels)

    countYesTest = 0
    for label in testLabels:
        if label == 1:
            countYesTest += 1
        
    testProportion = countYesTest / len(testLabels)

    allProportion = (countYesTraining + countYesTest) / (len(trainLabels) + len(testLabels))

    return (trainProportion, testProportion, allProportion)

# TODO (medium priority): Implement a proper argument parser

# Data manager parameters
resplit = True if '--resplit' in sys.argv else False

# Model parameters
retrain = True if '--retrain' in sys.argv else False 
save = True if '--save' in sys.argv else False
functional = True if '--functional' in sys.argv else False
use_bert = True if '--use-bert' in sys.argv else False
retrain_bert_vectors = True if '--retrain-bert' in sys.argv else False

# Logging parameters
skipLogging = True if '--skip-logging' in sys.argv else False

# Config parameters
config = configparser.ConfigParser()
config.read('conf.txt')

EPOCHS = int(config['GENERAL']['EPOCHS'])
BATCH_SIZE = int(config['GENERAL']['BATCH_SIZE'])
training_set_ratio = float(config['GENERAL']['TRAINING_SET_RATIO'])
dataset_tsv_file = config['GENERAL']['DATASET_NAME']
use_kfold = True if config['GENERAL']['USE_KFOLD'] == 'true' else False
model_type = config['GENERAL']['MODEL_TYPE']

if resplit:
    new_dataset(dataset_tsv_file, training_set_ratio)

training_dataset_text, test_dataset_text, training_ex_emb, test_ex_emb = get_dataset()

training_tk_ids, test_tk_ids = np.array(get_bert_token_ids())

########## Train and Test Process ########## 
try:
    ft_model = load_model("models/fasttext_model/baseline.bin")
except ValueError as err:
    print(err)
    print("Couldn't find a saved model, aborting...")
    exit(0)

training_dataset_embeddings = dataset_to_embeddings(training_dataset_text, ft_model)
training_dataset_labels = np.asarray([int(ex[1]) for ex in training_dataset_text])

test_dataset_embeddings = dataset_to_embeddings(test_dataset_text, ft_model)
test_dataset_labels = np.asarray([int(ex[1]) for ex in test_dataset_text])

# YES label proportions
trainProportion, testProportion, allProportion = labelProportion(list(training_dataset_labels), list(test_dataset_labels))

# Dimension of the word embeddings                                                
example_dim = training_dataset_embeddings[0].shape

# Dimension of the tweet embeddings
tweet_emb_dim = training_ex_emb[0].shape

# Dimension of bert vectors
bert_dim = 0
bert_training_vectors = None
bert_test_vectors = None

if (use_bert):
    if (retrain_bert_vectors):
        # Dimension of bert tokens
        bert_tk_dim = training_tk_ids[0].shape

        bert = bert_model.BertModel(bert_tk_dim)

        bert_training_vectors = bert.predict(training_tk_ids)
        bert_test_vectors = bert.predict(test_tk_ids)

        np.savetxt('bert_training_vectors.txt', bert_training_vectors)
        np.savetxt('bert_test_vectors.txt', bert_test_vectors)
    else:
        bert_training_vectors = np.loadtxt('bert_training_vectors.txt')
        bert_test_vectors = np.loadtxt('bert_test_vectors.txt')

    bert_dim = bert_training_vectors[0].shape

training_dataset = None
test_dataset = None

if (model_type == 'classed'):
    tf.keras.backend.set_floatx('float64')

    training_dataset = tf.data.Dataset.from_tensor_slices((training_dataset_embeddings, training_dataset_labels))
    training_dataset = training_dataset.shuffle(400).batch(BATCH_SIZE, drop_remainder=True)

    test_dataset = tf.data.Dataset.from_tensor_slices((test_dataset_embeddings, test_dataset_labels))
    test_dataset = test_dataset.batch(BATCH_SIZE, drop_remainder=True)

    # Optimizer algorithm for training
    optimizer = tf.keras.optimizers.RMSprop()

    # Loss function
    loss_object = tf.keras.losses.BinaryCrossentropy(from_logits=True)

    # Metrics that will measure loss and accuracy of the model over the training process
    train_loss = tf.keras.metrics.Mean(name='train_loss')
    train_accuracy = tf.keras.metrics.BinaryAccuracy(name='train_accuracy')
    train_precision = tf.keras.metrics.Precision(name='train_precision', dtype='float32')
    train_recall = tf.keras.metrics.Recall(name='train_recall', dtype='float32')

    # Metrics that will measure loss and accuracy of the model over the testing process
    test_loss = tf.keras.metrics.Mean(name='test_loss')
    test_accuracy = tf.keras.metrics.BinaryAccuracy(name='test_accuracy')
    test_precision = tf.keras.metrics.Precision(name='test_precision', dtype='float32')
    test_recall = tf.keras.metrics.Recall(name='test_recall', dtype='float32')

    model = TfModel(example_dim,
                loss_object, 
                optimizer, 
                train_loss, 
                train_accuracy, 
                train_precision, 
                train_recall, 
                test_loss, 
                test_accuracy, 
                test_precision, 
                test_recall)

bestModel = None
confusion = None
if retrain:
    if (model_type == 'functional'):
        template = '\n###### Test results ######\n\nTest Loss: {},\nTest Accuracy: {},\nTest Precision: {},\nTest Recall: {},\nTest AUC: {},\nTest F-Score: {}\n'
        
        earlyStopping = tf.keras.callbacks.EarlyStopping(monitor='val_loss', 
                                                    patience=5,
                                                    restore_best_weights=True)

        if (use_kfold):
            num_folds = int(config['GENERAL']['NUM_FOLDS'])
            results = np.zeros((num_folds,6))

            dataset_embeddings = np.append(training_dataset_embeddings, test_dataset_embeddings, 0)
            dataset_ex_embeddings = np.append(training_ex_emb, test_ex_emb, 0)
            dataset_labels = np.append(training_dataset_labels, test_dataset_labels, 0)
            if(use_bert):
                dataset_embeddings_bert = np.append(bert_training_vectors, bert_test_vectors, 0)

            training_texts = [a[0] for a in training_dataset_text]
            test_texts = [a[0] for a in test_dataset_text]

            dataset_texts = list(training_texts)
            dataset_texts.extend(test_texts)

            splits = StratifiedKFold(num_folds).split(dataset_embeddings, dataset_labels)
            test_step = 0
            bestScore = 0
            bestTestInput = None
            bestTP = None
            bestTN = None
            bestFP = None
            bestFN = None
            bestTexts = None
            proportions = []
            for train_index, val_index in splits:
                model = FunctionalModel(example_dim, tweet_emb_dim, bert_dim, use_bert)
                
                training_inputs = [
                    np.asarray([dataset_embeddings[i] for i in train_index]),
                    np.asarray([dataset_ex_embeddings[i] for i in train_index])
                ]

                if (use_bert):
                    training_inputs.append(
                        np.asarray([dataset_embeddings_bert[i] for i in train_index])
                    )

                test_inputs = [
                    np.asarray([dataset_embeddings[i] for i in val_index]),
                    np.asarray([dataset_ex_embeddings[i] for i in val_index])
                ]

                if (use_bert):
                    test_inputs.append(
                        np.asarray([dataset_embeddings_bert[i] for i in val_index])
                    )

                training_labels = np.asarray([dataset_labels[i] for i in train_index])
                test_labels = np.asarray([dataset_labels[i] for i in val_index])
                proportions.append(labelProportion(training_labels, test_labels))

                history = model.fit(training_inputs, 
                                training_labels,
                                validation_split=0.2,
                                batch_size=BATCH_SIZE,
                                epochs=EPOCHS,
                                callbacks=[earlyStopping])

                loss, accuracy, precision, recall, auc, tp, tn, fp, fn = model.evaluate(test_inputs,
                                                    test_labels, 
                                                    batch_size=BATCH_SIZE, 
                                                    verbose=2)
                                                    
                results[test_step][0] = loss
                results[test_step][1] = accuracy
                results[test_step][2] = precision
                results[test_step][3] = recall
                results[test_step][4] = auc
                results[test_step][5] = 2 * (precision * recall) / (precision + recall)

                if results[test_step][5] > bestScore:
                    bestScore = results[test_step][5]
                    bestModel = model
                    bestTestInput = test_inputs
                    bestTestSet = test_labels
                    bestTP = tp
                    bestTN = tn
                    bestFP = fp
                    bestFN = fn
                    bestTexts = [dataset_texts[i] for i in val_index]

                test_step += 1

                tf.keras.backend.clear_session()
            
            mean_results = np.mean(results, axis=0)
            
            print(template.format(mean_results[0], 
                                mean_results[1], 
                                mean_results[2], 
                                mean_results[3], 
                                mean_results[4], 
                                mean_results[5]))
            
            loss = mean_results[0]
            accuracy = mean_results[1]
            precision = mean_results[2]
            recall = mean_results[3]
            auc = mean_results[4]
            fscore = mean_results[5]
            
            predictions = bestModel.predict(bestTestInput)

            predictionsFile = open(f'results/predictions{str(math.trunc(time.time()))}.txt', 'w')

            with open('datasets/idorsPP.tsv') as tsvFile:
                reader = csv.DictReader(tsvFile, dialect='excel-tab')
                for r in reader:
                    if r['pretext']:
                        try:
                            i = bestTexts.index(r['pretext'])
                            predictionsFile.write(r['id'] + " || " + r['HS'] + " || " + r['OF'] + " || " + r['HT'] + " || " + str(predictions[i][0]) + " || " + r['text'] + "\n")
                        except:
                            pass

        else:
            model = FunctionalModel(example_dim, tweet_emb_dim, bert_dim, use_bert)

            training_inputs = [training_dataset_embeddings, training_ex_emb]

            if (use_bert):
                training_inputs.append(bert_training_vectors)
            
            history = model.fit(training_inputs, 
                                training_dataset_labels,
                                batch_size=BATCH_SIZE,
                                epochs=EPOCHS,
                                validation_split=0.2,
                                callbacks=[earlyStopping])

            test_inputs = [test_dataset_embeddings, test_ex_emb]

            if (use_bert):
                test_inputs.append(bert_test_vectors)

            #TODO (low priority): Make an evaluate method for the subclassed model
            loss, accuracy, precision, recall, auc = model.evaluate(test_inputs,
                                                                test_dataset_labels, 
                                                                batch_size=BATCH_SIZE, 
                                                                verbose=2)

            fscore = 2 * (precision * recall) / (precision + recall)
        
            print(template.format(loss, accuracy, precision, recall, auc, fscore))

        if not skipLogging:
            logDir = config['GENERAL']['LOGDIR']
            directory = logDir + '/' + date.today().strftime("%m-%d-%Y")
            Path(directory).mkdir(parents=True, exist_ok=True)
            with open(directory + '/' + dataset_tsv_file.split('.tsv')[0] + str(math.trunc(time.time())), 'w') as logfile:
                logfile.write('Using dataset: ' + dataset_tsv_file + '\n\n')
                logfile.write('Training dataset size: {}\n'.format(len(training_dataset_embeddings)))
                logfile.write('Test dataset size: {}\n'.format(len(test_dataset_embeddings)))
                logfile.write('\n###### Positive label proportion ######\n\n')
                for i, p in enumerate(proportions):
                    logfile.write('For training in fold {}: {}\n'.format(i, p[0]))
                    logfile.write('For test in fold {}: {}\n'.format(i, p[1]))
                logfile.write('For combined dataset: {}\n'.format(proportions[0][2]))   
                logfile.write('\n###### Fold results ######\n\n')
                for r in results:
                    logfile.write('loss:{}\n'.format(r[0]))
                    logfile.write('accuracy:{}\n'.format(r[1]))
                    logfile.write('precision:{}\n'.format(r[2]))
                    logfile.write('recall:{}\n'.format(r[3]))
                    logfile.write('AUC:{}\n'.format(r[4]))
                    logfile.write('fscore:{}\n\n'.format(r[5]))
                logfile.write('\n###### Model Summary ######\n\n')
                model.summary(print_fn=lambda x: logfile.write(x + '\n'))
                logfile.write(template.format(loss, accuracy, precision, recall, auc, fscore))
                logfile.write('TP:{}\n'.format(bestTP))
                logfile.write('TN:{}\n'.format(bestTN))
                logfile.write('FP:{}\n'.format(bestFP))
                logfile.write('FN:{}\n'.format(bestFN))
                logfile.write('\n###### Metrics history for {} epochs: ######\n\n'.format(len(history.epoch)))
                for epoch in history.epoch:
                    metricsHistory = history.history
                    logfile.write('Epoch {}: '.format(epoch + 1))
                    for key in metricsHistory.keys():
                        logfile.write('{}: {},'.format(key, metricsHistory[key][epoch]))
                        logfile.write(' ')
                    logfile.write('\n')
                logfile.write('\n##### Raw metrics history #####\n\n')
                pprint(metricsHistory, logfile)

        if save:
            directory = "saved_models"
            Path(directory).mkdir(parents=True, exist_ok=True)
            bestModel.save_weights(directory + '/' + model_type + str(math.trunc(time.time())))
    elif (model_type == 'svm'):
        template = '\n###### Test results ######\n\nTest Accuracy: {},\nTest Precision: {},\nTest Recall: {},\nTest F-Score: {}\n'
        if (use_kfold):
            num_folds = int(config['GENERAL']['NUM_FOLDS'])
            results = np.zeros((num_folds,4))

            dataset_embeddings = np.append(training_dataset_embeddings, test_dataset_embeddings, 0)
            dataset_ex_embeddings = np.append(training_ex_emb, test_ex_emb, 0)
            dataset_bert_vectors = np.append(bert_training_vectors, bert_test_vectors, 0)
            dataset_labels = np.append(training_dataset_labels, test_dataset_labels, 0)

            training_texts = [a[0] for a in training_dataset_text]
            test_texts = [a[0] for a in test_dataset_text]

            dataset_texts = list(training_texts)
            dataset_texts.extend(test_texts)

            splits = StratifiedKFold(num_folds).split(dataset_embeddings, dataset_labels)
            test_step = 0
            bestScore = 0
            bestTestSet = None
            bestTestInput = None
            bestTP = None
            bestTN = None
            bestFP = None
            bestFN = None
            bestTexts = None
            proportions = []
            for train_index, val_index in splits:
                model = SVM()

                training_dataset_embeddings = np.asarray([dataset_embeddings[i] for i in train_index])
                training_ex_emb = np.asarray([dataset_ex_embeddings[i] for i in train_index])

                training_embeddings_bert = []
                if (use_bert):
                    training_embeddings_bert = np.asarray([dataset_bert_vectors[i] for i in train_index])

                test_dataset_embeddings = np.asarray([dataset_embeddings[i] for i in val_index])
                test_ex_emb = np.asarray([dataset_ex_embeddings[i] for i in val_index])

                test_embeddings_bert = []
                if (use_bert):
                    test_embeddings_bert = np.asarray([dataset_bert_vectors[i] for i in val_index])

                training_labels = np.asarray([dataset_labels[i] for i in train_index])
                test_labels = np.asarray([dataset_labels[i] for i in val_index])
                proportions.append(labelProportion(training_labels, test_labels))

                model.fit(training_dataset_embeddings, training_ex_emb, training_embeddings_bert, training_labels)
                accuracy, precision, recall, fscore, tp, tn, fp, fn = model.evaluate(test_dataset_embeddings, test_ex_emb, test_embeddings_bert, test_labels)
                                                    
                results[test_step][0] = accuracy
                results[test_step][1] = precision
                results[test_step][2] = recall
                results[test_step][3] = fscore

                if results[test_step][3] > bestScore:
                    bestScore = results[test_step][3]
                    bestModel = model
                    bestTestSet = test_labels
                    bestTestInput = [test_dataset_embeddings, test_ex_emb, test_embeddings_bert]
                    bestTP = tp
                    bestTN = tn
                    bestFP = fp
                    bestFN = fn
                    bestTexts = [dataset_texts[i] for i in val_index]
                
                test_step += 1
            
            mean_results = np.mean(results, axis=0)
            
            accuracy = mean_results[0]
            precision = mean_results[1]
            recall = mean_results[2]
            fscore = mean_results[3]

            predictions = bestModel.predict(bestTestInput[0], bestTestInput[1], bestTestInput[2])

            predictionsFile = open(f'results/predictions{str(math.trunc(time.time()))}.txt', 'w')

            with open('datasets/idorsPP.tsv') as tsvFile:
                reader = csv.DictReader(tsvFile, dialect='excel-tab')
                for r in reader:
                    if r['pretext']:
                        try:
                            i = bestTexts.index(r['pretext'])
                            predictionsFile.write(r['id'] + " || " + r['HS'] + " || " + r['OF'] + " || " + r['HT'] + " || " + str(predictions[i]) + " || " + r['text'] + "\n")
                        except:
                            pass

            if not skipLogging:
                logDir = config['GENERAL']['LOGDIR']
                directory = logDir + '/' + date.today().strftime("%m-%d-%Y")
                Path(directory).mkdir(parents=True, exist_ok=True)
                with open(directory + '/' + dataset_tsv_file.split('.tsv')[0] + str(math.trunc(time.time())), 'w') as logfile:
                    logfile.write('Using dataset: ' + dataset_tsv_file + '\n\n')
                    logfile.write('Training dataset size: {}\n'.format(len(training_dataset_embeddings)))
                    logfile.write('Test dataset size: {}\n'.format(len(test_dataset_embeddings)))
                    logfile.write('\n###### Positive label proportion ######\n\n')
                    for i, p in enumerate(proportions):
                        logfile.write('For training in fold {}: {}\n'.format(i, p[0]))
                        logfile.write('For test in fold {}: {}\n'.format(i, p[1]))
                    logfile.write('For combined dataset: {}\n'.format(proportions[0][2]))
                    logfile.write('\n###### Fold results ######\n\n')
                    for r in results:
                        logfile.write('accuracy:{}\n'.format(r[0]))
                        logfile.write('precision:{}\n'.format(r[1]))
                        logfile.write('recall:{}\n'.format(r[2]))
                        logfile.write('fscore:{}\n\n'.format(r[3]))
                    logfile.write(template.format(accuracy, precision, recall, fscore))
                    logfile.write('TP:{}\n'.format(bestTP))
                    logfile.write('TN:{}\n'.format(bestTN))
                    logfile.write('FP:{}\n'.format(bestFP))
                    logfile.write('FN:{}\n'.format(bestFN))
            
            if save:
                directory = "saved_models"
                Path(directory).mkdir(parents=True, exist_ok=True)
                bestModel.save_weights(directory + '/' + model_type + str(math.trunc(time.time())))
        else:
            model.fit(training_dataset_embeddings, training_ex_emb, training_dataset_labels)
            model.evaluate(test_dataset_embeddings, test_ex_emb, test_dataset_labels)
    elif (model_type == 'classed'):
        model.fit(training_dataset, test_dataset, EPOCHS)
else:
    try:
        model.load_weights('tf_weights.h5')
    except ImportError as h5_err:
        print(h5_err)
        print("You need to install h5py to load a TensorFlow model, aborting...")
    except IOError as io_err:
        print(io_err)
        print("Couldn't find a saved TensorFlow model, aborting...")
        